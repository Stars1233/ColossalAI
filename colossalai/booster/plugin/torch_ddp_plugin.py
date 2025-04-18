from typing import Callable, Dict, Iterator, List, Optional, Tuple, Union

import torch
import torch.nn as nn
from peft import PeftModel
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.optim import Optimizer
from torch.optim.lr_scheduler import _LRScheduler as LRScheduler
from torch.utils._pytree import tree_map
from torch.utils.data import DataLoader

from colossalai.checkpoint_io import CheckpointIO, GeneralCheckpointIO
from colossalai.cluster import DistCoordinator
from colossalai.interface import ModelWrapper, OptimizerWrapper
from colossalai.interface.model import PeftUnwrapMixin
from colossalai.logging import get_dist_logger
from colossalai.quantization import BnbQuantizationConfig, quantize_model
from colossalai.utils import get_current_device

from .dp_plugin_base import DPPluginBase

__all__ = ["TorchDDPPlugin"]


class TorchDDPCheckpointIO(GeneralCheckpointIO):
    def __init__(self) -> None:
        super().__init__()
        self.coordinator = DistCoordinator()
        self.logger = get_dist_logger()

    def load_unsharded_model(
        self,
        model: ModelWrapper,
        checkpoint: str,
        strict: bool = True,
        low_cpu_mem_mode: bool = True,
        num_threads: int = 1,
    ):
        """
        Load model from checkpoint.
        """
        assert isinstance(model, ModelWrapper), "Please boost the model before loading!"
        super().load_unsharded_model(
            model.unwrap(), checkpoint, strict=strict, low_cpu_mem_mode=low_cpu_mem_mode, num_threads=num_threads
        )

    def save_unsharded_model(
        self, model: ModelWrapper, checkpoint: str, gather_dtensor: bool, use_safetensors: bool, use_async: bool = False
    ):
        """
        Save model to checkpoint but only on master process.
        """
        assert isinstance(model, ModelWrapper), "Please boost the model before saving!"
        if self.coordinator.is_master():
            super().save_unsharded_model(
                model.unwrap(), checkpoint, gather_dtensor, use_safetensors, use_async=use_async
            )

    def load_unsharded_optimizer(
        self, optimizer: OptimizerWrapper, checkpoint: str, low_cpu_mem_mode: bool = True, num_threads: int = 1
    ):
        """
        Load optimizer from checkpoint.
        """
        assert isinstance(optimizer, OptimizerWrapper), "Please boost the optimizer before loading!"
        super().load_unsharded_optimizer(
            optimizer, checkpoint, low_cpu_mem_mode=low_cpu_mem_mode, num_threads=num_threads
        )

    def save_unsharded_optimizer(
        self, optimizer: OptimizerWrapper, checkpoint: str, gather_dtensor: bool, use_async: bool = False
    ):
        """
        Save optimizer to checkpoint but only on master process.
        """
        assert isinstance(optimizer, OptimizerWrapper), "Please boost the optimizer before saving!"
        if self.coordinator.is_master():
            super().save_unsharded_optimizer(optimizer, checkpoint, gather_dtensor, use_async=use_async)

    def save_lr_scheduler(self, lr_scheduler: LRScheduler, checkpoint: str):
        """
        Save model to checkpoint but only on master process.
        """
        if self.coordinator.is_master():
            super().save_lr_scheduler(lr_scheduler, checkpoint)

    def save_sharded_model(
        self,
        model: ModelWrapper,
        checkpoint_path: str,
        gather_dtensor: bool = True,
        prefix: Optional[str] = None,
        max_shard_size: int = 1024,
        use_safetensors: bool = False,
        use_async: bool = False,
    ):
        """
        Save model to checkpoint but only on master process.
        """
        assert isinstance(model, ModelWrapper), "Please boost the model before saving!"
        if self.coordinator.is_master():
            super().save_sharded_model(
                model.unwrap(),
                checkpoint_path,
                gather_dtensor,
                prefix,
                max_shard_size,
                use_safetensors,
                use_async=use_async,
            )

    def load_sharded_model(
        self,
        model: ModelWrapper,
        checkpoint_index_file: str,
        strict: bool = False,
        use_safetensors: bool = False,
        load_sub_module: bool = True,
        low_cpu_mem_mode: bool = True,
        num_threads: int = 1,
    ):
        """
        Load model from sharded checkpoint.
        """
        assert isinstance(model, ModelWrapper), "Please boost the model before loading!"
        super().load_sharded_model(
            model.unwrap(),
            checkpoint_index_file,
            strict,
            use_safetensors,
            load_sub_module,
            low_cpu_mem_mode=low_cpu_mem_mode,
            num_threads=num_threads,
        )

    def save_sharded_optimizer(
        self,
        optimizer: OptimizerWrapper,
        checkpoint: str,
        gather_dtensor: bool = True,
        prefix: Optional[str] = None,
        size_per_shard: int = 1024,
        use_async: bool = False,
    ):
        """
        Save optimizer to sharded checkpoint but only on master process.
        """
        assert isinstance(optimizer, OptimizerWrapper), "Please boost the optimizer before saving!"
        if self.coordinator.is_master():
            super().save_sharded_optimizer(
                optimizer.unwrap(), checkpoint, gather_dtensor, prefix, size_per_shard, use_async=use_async
            )

    def load_sharded_optimizer(
        self,
        optimizer: Optimizer,
        index_file_path: str,
        prefix: Optional[str] = None,
        low_cpu_mem_mode: bool = True,
        num_threads: int = 1,
    ):
        """
        Load optimizer from sharded checkpoint.
        """
        assert isinstance(optimizer, OptimizerWrapper), "Please boost the optimizer before loading!"
        super().load_sharded_optimizer(
            optimizer.unwrap(), index_file_path, prefix, low_cpu_mem_mode=low_cpu_mem_mode, num_threads=num_threads
        )

    def save_lora_as_pretrained(
        self,
        model: Union[nn.Module, ModelWrapper],
        checkpoint: str,
        use_safetensors: bool = False,
        state_dict: Optional[dict] = None,
    ) -> None:
        """
        Save the lora adapters and adapter configuration file to checkpoint directory.
        """
        from peft import PeftModel

        assert isinstance(model, ModelWrapper), "Please boost the model before saving!"
        peft_model = model.unwrap(unwrap_peft=False)
        assert isinstance(
            peft_model, PeftModel
        ), "The model doesn't have lora adapters, please enable lora before saving."
        if state_dict is None:
            state_dict = tree_map(lambda x: x.data.cpu() if torch.is_tensor(x) else x, peft_model.state_dict())
        if self.coordinator.is_master():
            return peft_model.save_pretrained(
                checkpoint,
                safe_serialization=use_safetensors,
                state_dict=state_dict,
            )


class TorchDDPModel(ModelWrapper):
    def __init__(self, module: nn.Module, *args, **kwargs) -> None:
        super().__init__(module)
        self.module = DDP(module, *args, **kwargs)

    def unwrap(self, unwrap_peft: bool = True) -> nn.Module:
        model = self.module.module
        if unwrap_peft and isinstance(model, PeftModel):
            model = PeftUnwrapMixin(model)
        return model


class TorchDDPPlugin(DPPluginBase):
    """
    Plugin for PyTorch DDP.

    ```python
    from colossalai.booster import Booster
    from colossalai.booster.plugin import TorchDDPPlugin

    model, train_dataset, optimizer, criterion = ...
    plugin = TorchDDPPlugin()

    train_dataloader = plugin.prepare_dataloader(train_dataset, batch_size=8)
    booster = Booster(plugin=plugin)
    model, optimizer, train_dataloader, criterion = booster.boost(model, optimizer, train_dataloader, criterion)
    ```

    Args:
        broadcast_buffers (bool, optional): Whether to broadcast buffers in the beginning of training. Defaults to True.
        bucket_cap_mb (int, optional): The bucket size in MB. Defaults to 25.
        find_unused_parameters (bool, optional): Whether to find unused parameters. Defaults to False.
        check_reduction (bool, optional): Whether to check reduction. Defaults to False.
        gradient_as_bucket_view (bool, optional): Whether to use gradient as bucket view. Defaults to False.
        static_graph (bool, optional): Whether to use static graph. Defaults to False.
        fp8_communication (bool, optional): Whether to enable fp8 communication. Defaults to False.
    """

    def __init__(
        self,
        broadcast_buffers: bool = True,
        bucket_cap_mb: int = 25,
        find_unused_parameters: bool = False,
        check_reduction: bool = False,
        gradient_as_bucket_view: bool = False,
        static_graph: bool = False,
        fp8_communication: bool = False,
    ) -> None:
        super().__init__()
        self.ddp_kwargs = dict(
            broadcast_buffers=broadcast_buffers,
            bucket_cap_mb=bucket_cap_mb,
            find_unused_parameters=find_unused_parameters,
            check_reduction=check_reduction,
            gradient_as_bucket_view=gradient_as_bucket_view,
            static_graph=static_graph,
        )
        self.fp8_communication = fp8_communication

    def support_no_sync(self) -> bool:
        return True

    def support_lora(self) -> bool:
        return True

    def control_precision(self) -> bool:
        return False

    def supported_precisions(self) -> List[str]:
        return ["fp16", "fp16_apex", "bf16", "fp8"]

    def control_device(self) -> bool:
        return True

    def supported_devices(self) -> List[str]:
        return ["cuda", "npu"]

    def configure(
        self,
        model: nn.Module,
        optimizer: Optional[Optimizer] = None,
        criterion: Optional[Callable] = None,
        dataloader: Optional[DataLoader] = None,
        lr_scheduler: Optional[LRScheduler] = None,
    ) -> Tuple[nn.Module, OptimizerWrapper, Callable, DataLoader, LRScheduler]:
        # cast model to cuda
        model = model.to(get_current_device())

        # convert model to sync bn
        model = nn.SyncBatchNorm.convert_sync_batchnorm(model, None)

        # wrap the model with PyTorch DDP
        model = TorchDDPModel(model, **self.ddp_kwargs)

        if optimizer is not None and not isinstance(optimizer, OptimizerWrapper):
            optimizer = OptimizerWrapper(optimizer)

        if self.fp8_communication:
            from colossalai.quantization.fp8 import fp8_compress_ddp_grad_comm_hook_async

            model.module.register_comm_hook(None, fp8_compress_ddp_grad_comm_hook_async)

        return model, optimizer, criterion, dataloader, lr_scheduler

    def control_checkpoint_io(self) -> bool:
        return True

    def get_checkpoint_io(self) -> CheckpointIO:
        return TorchDDPCheckpointIO()

    def no_sync(self, model: nn.Module, optimizer: OptimizerWrapper) -> Iterator[None]:
        assert isinstance(model, TorchDDPModel), "Model is not boosted by TorchDDPPlugin."
        return model.module.no_sync()

    def enable_lora(
        self,
        model: nn.Module,
        pretrained_dir: Optional[str] = None,
        lora_config: Optional[Dict] = None,
        bnb_quantization_config: Optional[BnbQuantizationConfig] = None,
    ) -> nn.Module:
        from peft import PeftModel, get_peft_model

        if bnb_quantization_config is not None:
            model = quantize_model(model, bnb_quantization_config)

        assert not isinstance(model, TorchDDPModel), "Lora should be enabled before boosting the model."
        if pretrained_dir is None:
            return get_peft_model(model, lora_config)
        else:
            return PeftModel.from_pretrained(model, pretrained_dir, is_trainable=True)
