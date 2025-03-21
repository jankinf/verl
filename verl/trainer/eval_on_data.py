import hydra
import numpy as np
import re
import torch
import torch.distributed
from pathlib import Path
from torch.utils.data import DataLoader
from transformers import AutoModelForCausalLM, AutoConfig
from collections import defaultdict

from verl import DataProto
from verl.utils import hf_tokenizer
from verl.utils.fs import copy_local_path_from_hdfs
from verl.protocol import pad_dataproto_to_divisor, unpad_dataproto
from verl.utils.dataset.rl_dataset import RLHFDataset, collate_fn
from verl.workers.reward_manager import NaiveRewardManager
from verl.workers.rollout.hf_rollout import HFRollout


def load_sharded_model(fsdp_checkpoint_path):
    state_dict = defaultdict(list)
    checkpoint_dir = Path(fsdp_checkpoint_path)

    shard_files = list(checkpoint_dir.glob("model_world_size_*_rank_*.pt"))
    if not shard_files:
        raise ValueError(f"No checkpoint files found in {fsdp_checkpoint_path}")

    pattern = re.compile(r"model_world_size_(\d+)_rank_(\d+)\.pt")
    world_sizes = set()
    for file in shard_files:
        match = pattern.match(file.name)
        if match:
            world_sizes.add(int(match.group(1)))

    if len(world_sizes) != 1:
        raise ValueError(
            f"Inconsistent world_size found in checkpoint files: {world_sizes}"
        )

    world_size = world_sizes.pop()
    print(f"Found checkpoints with world_size = {world_size}")

    for rank in range(world_size):
        filepath = checkpoint_dir / f"model_world_size_{world_size}_rank_{rank}.pt"
        if not filepath.exists():
            raise ValueError(f"Missing shard file: {filepath}")

        print(f"Loading shard: {filepath}")
        shard_dict = torch.load(filepath)

        for key, value in shard_dict.items():
            if hasattr(value, "to_local"):
                value = value.to_local()
            state_dict[key].append(value)

    consolidated_state_dict = {}
    for key in state_dict:
        try:
            consolidated_state_dict[key] = torch.cat(state_dict[key], dim=0)
        except (RuntimeError, TypeError):
            consolidated_state_dict[key] = state_dict[key][0]
            print(
                f"Parameter '{key}' does not need concatenation, using first shard value"
            )

    return consolidated_state_dict


def initialize_model_and_tokenizer(
    model_path, trust_remote_code=True, torch_dtype=torch.bfloat16
):
    local_path = copy_local_path_from_hdfs(model_path)
    tokenizer = hf_tokenizer(local_path, trust_remote_code=trust_remote_code)

    actor_model_config = AutoConfig.from_pretrained(
        local_path, trust_remote_code=trust_remote_code
    )
    actor_module = AutoModelForCausalLM.from_pretrained(
        pretrained_model_name_or_path=local_path,
        torch_dtype=torch_dtype,
        config=actor_model_config,
        attn_implementation="flash_attention_2",
        trust_remote_code=trust_remote_code,
    )

    return tokenizer, actor_module


@hydra.main(config_path='config', config_name='eval_on_data', version_base=None)
def main(config):
    # Loading huggingface-style checkpoint, for example "Qwen/Qwen2.5-3B" or local_ckpt_path
    model_path = config.actor_rollout_ref.model.hf_model_path
    tokenizer, actor_module = initialize_model_and_tokenizer(model_path)

    # Loading FSDP checkpoint (optional: these three lines can be skipped. Prerequisite: actor_module must be preloaded)
    fsdp_checkpoint_path = config.actor_rollout_ref.model.get("fsdp_checkpoint_path", None)
    if fsdp_checkpoint_path is not None:
        state_dict = load_sharded_model(fsdp_checkpoint_path)
        actor_module.load_state_dict(state_dict)

    actor_module.to(torch.bfloat16)
    actor_module.to("cuda:0")

    val_files = config.data.val_files
    val_dataset = RLHFDataset(
        parquet_files=val_files,
        tokenizer=tokenizer,
        prompt_key="prompt",
        max_prompt_length=config.data.max_prompt_length,
        filter_prompts=True,
        return_raw_chat=False,
        truncation="error",
    )
    val_dataloader = DataLoader(
        dataset=val_dataset,
        batch_size=config.data.batch_size,
        shuffle=config.data.shuffle,
        drop_last=config.data.drop_last,
        collate_fn=collate_fn,
    )

    assert len(val_dataloader) >= 1

    val_reward_fn = NaiveRewardManager(
        tokenizer=tokenizer, num_examine=1, compute_score=None
    )

    hfrollout = HFRollout(module=actor_module, config=config)

    sample_inputs = []
    sample_outputs = []
    sample_scores = []
    reward_tensor_lst = []
    data_source_lst = []

    for data in val_dataloader:
        test_batch = DataProto.from_single_dict(data)
        test_batch = test_batch.to("cuda")
        input_ids = test_batch.batch["input_ids"]
        input_texts = [
            tokenizer.decode(ids, skip_special_tokens=True) for ids in input_ids
        ]
        sample_inputs.extend(input_texts)

        test_gen_batch = test_batch.pop(["input_ids", "attention_mask", "position_ids"])
        test_gen_batch.meta_info = {
            "eos_token_id": tokenizer.eos_token_id,
            "pad_token_id": tokenizer.pad_token_id,
            "recompute_log_prob": False,
            "do_sample": False,
            "validate": True,
        }

        test_gen_batch_padded, pad_size = pad_dataproto_to_divisor(test_gen_batch, 1)
        test_output_gen_batch_padded = hfrollout.generate_sequences(
            test_gen_batch_padded
        )
        test_output_gen_batch = unpad_dataproto(
            test_output_gen_batch_padded, pad_size=pad_size
        )
        print("validation generation end")

        output_ids = test_output_gen_batch.batch["responses"]
        output_texts = [
            tokenizer.decode(ids, skip_special_tokens=True) for ids in output_ids
        ]
        sample_outputs.extend(output_texts)
        test_batch = test_batch.union(test_output_gen_batch)

        reward_tensor = val_reward_fn(test_batch)
        scores = reward_tensor.sum(-1).cpu().tolist()
        sample_scores.extend(scores)
        reward_tensor_lst.append(reward_tensor)
        data_source_lst.append(
            test_batch.non_tensor_batch.get(
                "data_source", ["unknown"] * reward_tensor.shape[0]
            )
        )

    reward_tensor = torch.cat(reward_tensor_lst, dim=0).sum(-1).cpu()
    data_sources = np.concatenate(data_source_lst, axis=0)

    data_source_reward = {}
    for i in range(reward_tensor.shape[0]):
        data_source = data_sources[i]
        if data_source not in data_source_reward:
            data_source_reward[data_source] = []
        data_source_reward[data_source].append(reward_tensor[i].item())

    metric_dict = {}
    for data_source, rewards in data_source_reward.items():
        metric_dict[f"val/test_score/{data_source}"] = np.mean(rewards)

    print(metric_dict)


if __name__ == "__main__":
    main()
