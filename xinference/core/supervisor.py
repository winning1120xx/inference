# Copyright 2022-2023 XProbe Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import asyncio
import itertools
import os
import signal
import time
import typing
from collections import defaultdict
from dataclasses import dataclass, field
from logging import getLogger
from typing import (
    TYPE_CHECKING,
    Any,
    DefaultDict,
    Dict,
    Iterator,
    List,
    Literal,
    Optional,
    Tuple,
    Union,
)

import xoscar as xo

from ..constants import (
    XINFERENCE_DEFAULT_CANCEL_BLOCK_DURATION,
    XINFERENCE_DISABLE_HEALTH_CHECK,
    XINFERENCE_HEALTH_CHECK_FAILURE_THRESHOLD,
    XINFERENCE_HEALTH_CHECK_INTERVAL,
    XINFERENCE_HEALTH_CHECK_TIMEOUT,
)
from ..core.model import ModelActor
from ..core.status_guard import InstanceInfo, LaunchStatus
from ..model.utils import get_engine_params_by_name
from ..types import PeftModelConfig
from .metrics import record_metrics
from .resource import GPUStatus, ResourceStatus
from .utils import (
    assign_replica_gpu,
    build_replica_model_uid,
    gen_random_string,
    is_valid_model_uid,
    iter_replica_model_uid,
    log_async,
    log_sync,
    parse_model_version,
    parse_replica_model_uid,
)

if TYPE_CHECKING:
    from ..model.audio import AudioModelFamilyV2
    from ..model.embedding import EmbeddingModelFamilyV2
    from ..model.flexible import FlexibleModelSpec
    from ..model.image import ImageModelFamilyV2
    from ..model.llm import LLMFamilyV2
    from ..model.rerank import RerankModelFamilyV2
    from ..model.video import VideoModelFamilyV2
    from .worker import WorkerActor


logger = getLogger(__name__)


ASYNC_LAUNCH_TASKS = {}  # type: ignore


def callback_for_async_launch(model_uid: str):
    ASYNC_LAUNCH_TASKS.pop(model_uid, None)
    logger.debug(f"Model uid: {model_uid} async launch completes.")


@dataclass
class WorkerStatus:
    update_time: float
    failure_remaining_count: int
    status: Dict[str, Union[ResourceStatus, GPUStatus]]


@dataclass
class ReplicaInfo:
    replica: int
    scheduler: Iterator
    replica_to_worker_refs: DefaultDict[int, List[xo.ActorRefType["WorkerActor"]]] = (
        field(default_factory=lambda: defaultdict(list))
    )


class SupervisorActor(xo.StatelessActor):
    def __init__(self):
        super().__init__()
        self._worker_address_to_worker: Dict[str, xo.ActorRefType["WorkerActor"]] = {}  # type: ignore
        self._worker_status: Dict[str, WorkerStatus] = {}  # type: ignore
        self._replica_model_uid_to_worker: Dict[  # type: ignore
            str,
            Union[
                xo.ActorRefType["WorkerActor"],
                Tuple[xo.ActorRefType["WorkerActor"], ...],
            ],
        ] = {}
        self._model_uid_to_replica_info: Dict[str, ReplicaInfo] = {}  # type: ignore
        self._uptime = None
        self._lock = asyncio.Lock()

    @classmethod
    def default_uid(cls) -> str:
        return "supervisor"

    def _get_worker_ref_by_ip(
        self, ip: str
    ) -> Optional[xo.ActorRefType["WorkerActor"]]:
        for addr, ref in self._worker_address_to_worker.items():
            existing_ip = addr.split(":")[0]
            if existing_ip == ip:
                return ref
        return None

    async def __post_create__(self):
        self._uptime = time.time()
        if not XINFERENCE_DISABLE_HEALTH_CHECK:
            # Run _check_dead_nodes() in a dedicated thread.
            from ..isolation import Isolation

            self._isolation = Isolation(asyncio.new_event_loop(), threaded=True)
            self._isolation.start()
            asyncio.run_coroutine_threadsafe(
                self._check_dead_nodes(), loop=self._isolation.loop
            )
        logger.info(f"Xinference supervisor {self.address} started")
        from .cache_tracker import CacheTrackerActor
        from .progress_tracker import ProgressTrackerActor
        from .status_guard import StatusGuardActor

        self._status_guard_ref: xo.ActorRefType["StatusGuardActor"] = (  # type: ignore
            await xo.create_actor(
                StatusGuardActor,
                address=self.address,
                uid=StatusGuardActor.default_uid(),
            )
        )
        self._cache_tracker_ref: xo.ActorRefType[  # type: ignore
            "CacheTrackerActor"
        ] = await xo.create_actor(
            CacheTrackerActor, address=self.address, uid=CacheTrackerActor.default_uid()
        )
        self._progress_tracker: xo.ActorRefType[  # type: ignore
            "ProgressTrackerActor"
        ] = await xo.create_actor(
            ProgressTrackerActor,
            address=self.address,
            uid=ProgressTrackerActor.default_uid(),
        )

        from .event import EventCollectorActor

        self._event_collector_ref: xo.ActorRefType[  # type: ignore
            EventCollectorActor
        ] = await xo.create_actor(
            EventCollectorActor,
            address=self.address,
            uid=EventCollectorActor.default_uid(),
        )

        from ..model.audio import (
            CustomAudioModelFamilyV2,
            generate_audio_description,
            get_audio_model_descriptions,
            register_audio,
            unregister_audio,
        )
        from ..model.embedding import (
            CustomEmbeddingModelFamilyV2,
            generate_embedding_description,
            get_embedding_model_descriptions,
            register_embedding,
            unregister_embedding,
        )
        from ..model.flexible import (
            FlexibleModelSpec,
            generate_flexible_model_description,
            get_flexible_model_descriptions,
            register_flexible_model,
            unregister_flexible_model,
        )
        from ..model.image import (
            CustomImageModelFamilyV2,
            generate_image_description,
            get_image_model_descriptions,
            register_image,
            unregister_image,
        )
        from ..model.llm import (
            CustomLLMFamilyV2,
            generate_llm_version_info,
            get_llm_version_infos,
            register_llm,
            unregister_llm,
        )
        from ..model.rerank import (
            CustomRerankModelFamilyV2,
            generate_rerank_description,
            get_rerank_model_descriptions,
            register_rerank,
            unregister_rerank,
        )

        self._custom_register_type_to_cls: Dict[str, Tuple] = {  # type: ignore
            "LLM": (
                CustomLLMFamilyV2,
                register_llm,
                unregister_llm,
                generate_llm_version_info,
            ),
            "embedding": (
                CustomEmbeddingModelFamilyV2,
                register_embedding,
                unregister_embedding,
                generate_embedding_description,
            ),
            "rerank": (
                CustomRerankModelFamilyV2,
                register_rerank,
                unregister_rerank,
                generate_rerank_description,
            ),
            "image": (
                CustomImageModelFamilyV2,
                register_image,
                unregister_image,
                generate_image_description,
            ),
            "audio": (
                CustomAudioModelFamilyV2,
                register_audio,
                unregister_audio,
                generate_audio_description,
            ),
            "flexible": (
                FlexibleModelSpec,
                register_flexible_model,
                unregister_flexible_model,
                generate_flexible_model_description,
            ),
        }

        # record model version
        model_version_infos: Dict[str, List[Dict]] = {}  # type: ignore
        model_version_infos.update(get_llm_version_infos())
        model_version_infos.update(get_embedding_model_descriptions())
        model_version_infos.update(get_rerank_model_descriptions())
        model_version_infos.update(get_image_model_descriptions())
        model_version_infos.update(get_audio_model_descriptions())
        model_version_infos.update(get_flexible_model_descriptions())
        await self._cache_tracker_ref.record_model_version(
            model_version_infos, self.address
        )

        # Windows does not have signal handler
        if os.name != "nt":

            async def signal_handler():
                os._exit(0)

            loop = asyncio.get_running_loop()
            loop.add_signal_handler(
                signal.SIGTERM, lambda: asyncio.create_task(signal_handler())
            )

        from ..model.llm.vllm.xavier.block_tracker import VLLMBlockTracker
        from ..model.llm.vllm.xavier.collective_manager import CollectiveManager

        self._block_tracker_mapping: Dict[str, xo.ActorRefType[VLLMBlockTracker]] = {}  # type: ignore
        self._collective_manager_mapping: Dict[  # type: ignore
            str, xo.ActorRefType[CollectiveManager]
        ] = {}

    @typing.no_type_check
    async def get_cluster_device_info(self, detailed: bool = False) -> List:
        import psutil

        supervisor_device_info = {
            "ip_address": self.address.split(":")[0],
            "gpu_count": 0,
            "gpu_vram_total": 0,
        }
        if detailed:
            supervisor_device_info["gpu_vram_total"] = 0
            supervisor_device_info["gpu_vram_available"] = 0
            supervisor_device_info["cpu_available"] = psutil.cpu_count() * (
                1 - psutil.cpu_percent() / 100.0
            )
            supervisor_device_info["cpu_count"] = psutil.cpu_count()
            mem_info = psutil.virtual_memory()
            supervisor_device_info["mem_used"] = mem_info.used
            supervisor_device_info["mem_available"] = mem_info.available
            supervisor_device_info["mem_total"] = mem_info.total
        res = [{"node_type": "Supervisor", **supervisor_device_info}]
        for worker_addr, worker_status in self._worker_status.items():
            vram_total: float = sum(
                [v.mem_total for k, v in worker_status.status.items() if k != "cpu"]  # type: ignore
            )
            total = (
                vram_total if vram_total == 0 else f"{int(vram_total / 1024 / 1024)}MiB"
            )
            info = {
                "node_type": "Worker",
                "ip_address": worker_addr.split(":")[0],
                "gpu_count": len(worker_status.status) - 1,
                "gpu_vram_total": total,
            }
            if detailed:
                cpu_info = worker_status.status["cpu"]
                info["cpu_available"] = cpu_info.total * (1 - cpu_info.usage)
                info["cpu_count"] = cpu_info.total
                info["mem_used"] = cpu_info.memory_used
                info["mem_available"] = cpu_info.memory_available
                info["mem_total"] = cpu_info.memory_total
                info["gpu_vram_total"] = vram_total
                info["gpu_vram_available"] = sum(
                    [v.mem_free for k, v in worker_status.status.items() if k != "cpu"]
                )
            res.append(info)
        return res

    @staticmethod
    async def get_builtin_prompts() -> Dict[str, Any]:
        from ..model.llm.llm_family import BUILTIN_LLM_PROMPT_STYLE

        return {k: v for k, v in BUILTIN_LLM_PROMPT_STYLE.items()}

    @staticmethod
    async def get_builtin_families() -> Dict[str, List[str]]:
        from ..model.llm.llm_family import (
            BUILTIN_LLM_FAMILIES,
            BUILTIN_LLM_MODEL_CHAT_FAMILIES,
            BUILTIN_LLM_MODEL_GENERATE_FAMILIES,
            BUILTIN_LLM_MODEL_TOOL_CALL_FAMILIES,
        )

        to_filter_abilities = ["vision", "reasoning", "audio", "omni", "hybrid"]
        ability_to_names: Dict[str, List[str]] = {
            ability: [] for ability in to_filter_abilities
        }
        for family in BUILTIN_LLM_FAMILIES:
            for ability in to_filter_abilities:
                if ability in family.model_ability:
                    ability_to_names[ability].append(family.model_name)

        return {
            "chat": list(BUILTIN_LLM_MODEL_CHAT_FAMILIES),
            "generate": list(BUILTIN_LLM_MODEL_GENERATE_FAMILIES),
            "tools": list(BUILTIN_LLM_MODEL_TOOL_CALL_FAMILIES),
            **ability_to_names,
        }

    async def get_devices_count(self) -> int:
        from ..device_utils import gpu_count

        if self.is_local_deployment():
            return gpu_count()
        # distributed deployment, choose a worker and return its device_count.
        # Assume that each worker has the same count of cards.
        worker_ref = await self._choose_worker()
        return await worker_ref.get_devices_count()

    async def _choose_worker(
        self, available_workers: Optional[List[str]] = None
    ) -> xo.ActorRefType["WorkerActor"]:
        # TODO: better allocation strategy.
        min_running_model_count = None
        target_worker = None

        for worker_addr, worker in self._worker_address_to_worker.items():
            if available_workers and worker_addr not in available_workers:
                continue
            running_model_count = await worker.get_model_count()
            if (
                min_running_model_count is None
                or running_model_count < min_running_model_count
            ):
                min_running_model_count = running_model_count
                target_worker = worker

        if target_worker:
            return target_worker

        raise RuntimeError("No available worker found")

    @log_sync(logger=logger)
    def get_status(self) -> Dict:
        return {
            "uptime": int(time.time() - self._uptime),
            "workers": self._worker_status,
        }

    async def _to_llm_reg(
        self, llm_family: "LLMFamilyV2", is_builtin: bool
    ) -> Dict[str, Any]:
        from ..model.llm.cache_manager import LLMCacheManager

        instance_cnt = await self.get_instance_count(llm_family.model_name)
        version_cnt = await self.get_model_version_count(llm_family.model_name)

        if self.is_local_deployment():
            specs = []
            # TODO: does not work when the supervisor and worker are running on separate nodes.
            _llm_family = llm_family.copy()
            for spec in [
                _spec
                for _spec in llm_family.model_specs
                if _spec.model_hub == "huggingface"
            ]:
                _llm_family.model_specs = [spec]
                cache_manager = LLMCacheManager(_llm_family)
                specs.append(
                    {**spec.dict(), "cache_status": cache_manager.get_cache_status()}
                )
            res = {**llm_family.dict(), "is_builtin": is_builtin, "model_specs": specs}
        else:
            res = {**llm_family.dict(), "is_builtin": is_builtin}
        res["model_version_count"] = version_cnt
        res["model_instance_count"] = instance_cnt
        return res

    async def _to_embedding_model_reg(
        self, model_family: "EmbeddingModelFamilyV2", is_builtin: bool
    ) -> Dict[str, Any]:
        from ..model.embedding.cache_manager import EmbeddingCacheManager

        instance_cnt = await self.get_instance_count(model_family.model_name)
        version_cnt = await self.get_model_version_count(model_family.model_name)

        if self.is_local_deployment():
            _family = model_family.copy()
            specs = []
            # TODO: does not work when the supervisor and worker are running on separate nodes.
            for spec in [
                x for x in model_family.model_specs if x.model_hub == "huggingface"
            ]:
                _family.model_specs = [spec]
                specs.append(
                    {
                        **spec.dict(),
                        "cache_status": EmbeddingCacheManager(
                            _family
                        ).get_cache_status(),
                    }
                )
            res = {
                **model_family.dict(),
                "is_builtin": is_builtin,
                "model_specs": specs,
            }
        else:
            res = {
                **model_family.dict(),
                "is_builtin": is_builtin,
            }
        res["model_version_count"] = version_cnt
        res["model_instance_count"] = instance_cnt
        return res

    async def _to_rerank_model_reg(
        self, model_spec: "RerankModelFamilyV2", is_builtin: bool
    ) -> Dict[str, Any]:
        from ..model.cache_manager import CacheManager

        instance_cnt = await self.get_instance_count(model_spec.model_name)
        version_cnt = await self.get_model_version_count(model_spec.model_name)
        cache_manager = CacheManager(model_spec)

        if self.is_local_deployment():
            # TODO: does not work when the supervisor and worker are running on separate nodes.
            cache_status = cache_manager.get_cache_status()
            res = {
                **model_spec.dict(),
                "cache_status": cache_status,
                "is_builtin": is_builtin,
            }
        else:
            res = {
                **model_spec.dict(),
                "is_builtin": is_builtin,
            }
        res["model_version_count"] = version_cnt
        res["model_instance_count"] = instance_cnt
        return res

    async def _to_image_model_reg(
        self, model_family: "ImageModelFamilyV2", is_builtin: bool
    ) -> Dict[str, Any]:
        from ..model.image.cache_manager import ImageCacheManager

        instance_cnt = await self.get_instance_count(model_family.model_name)
        version_cnt = await self.get_model_version_count(model_family.model_name)

        if self.is_local_deployment():
            # TODO: does not work when the supervisor and worker are running on separate nodes.
            cache_manager = ImageCacheManager(model_family)
            res = {
                **model_family.dict(),
                "cache_status": cache_manager.get_cache_status(),
                "is_builtin": is_builtin,
            }
        else:
            res = {
                **model_family.dict(),
                "is_builtin": is_builtin,
            }
        res["model_version_count"] = version_cnt
        res["model_instance_count"] = instance_cnt
        return res

    async def _to_audio_model_reg(
        self, model_family: "AudioModelFamilyV2", is_builtin: bool
    ) -> Dict[str, Any]:
        from ..model.cache_manager import CacheManager

        instance_cnt = await self.get_instance_count(model_family.model_name)
        version_cnt = await self.get_model_version_count(model_family.model_name)
        cache_manager = CacheManager(model_family)

        if self.is_local_deployment():
            # TODO: does not work when the supervisor and worker are running on separate nodes.
            res = {
                **model_family.dict(),
                "cache_status": cache_manager.get_cache_status(),
                "is_builtin": is_builtin,
            }
        else:
            res = {
                **model_family.dict(),
                "is_builtin": is_builtin,
            }
        res["model_version_count"] = version_cnt
        res["model_instance_count"] = instance_cnt
        return res

    async def _to_video_model_reg(
        self, model_family: "VideoModelFamilyV2", is_builtin: bool
    ) -> Dict[str, Any]:
        from ..model.cache_manager import CacheManager

        instance_cnt = await self.get_instance_count(model_family.model_name)
        version_cnt = await self.get_model_version_count(model_family.model_name)
        cache_manager = CacheManager(model_family)

        if self.is_local_deployment():
            # TODO: does not work when the supervisor and worker are running on separate nodes.
            res = {
                **model_family.dict(),
                "cache_status": cache_manager.get_cache_status(),
                "is_builtin": is_builtin,
            }
        else:
            res = {
                **model_family.dict(),
                "is_builtin": is_builtin,
            }
        res["model_version_count"] = version_cnt
        res["model_instance_count"] = instance_cnt
        return res

    async def _to_flexible_model_reg(
        self, model_spec: "FlexibleModelSpec", is_builtin: bool
    ) -> Dict[str, Any]:
        instance_cnt = await self.get_instance_count(model_spec.model_name)
        version_cnt = await self.get_model_version_count(model_spec.model_name)

        if self.is_local_deployment():
            res = {
                **model_spec.dict(),
                "cache_status": True,
                "is_builtin": is_builtin,
            }
        else:
            res = {
                **model_spec.dict(),
                "is_builtin": is_builtin,
            }
        res["model_version_count"] = version_cnt
        res["model_instance_count"] = instance_cnt
        return res

    @log_async(logger=logger)
    async def list_model_registrations(
        self, model_type: str, detailed: bool = False
    ) -> List[Dict[str, Any]]:
        def sort_helper(item):
            assert isinstance(item["model_name"], str)
            return item.get("model_name").lower()

        ret = []
        if not self.is_local_deployment():
            workers = list(self._worker_address_to_worker.values())
            for worker in workers:
                ret.extend(await worker.list_model_registrations(model_type, detailed))

        if model_type == "LLM":
            from ..model.llm import BUILTIN_LLM_FAMILIES, get_user_defined_llm_families

            for family in BUILTIN_LLM_FAMILIES:
                if detailed:
                    ret.append(await self._to_llm_reg(family, True))
                else:
                    ret.append({"model_name": family.model_name, "is_builtin": True})

            for family in get_user_defined_llm_families():
                if detailed:
                    ret.append(await self._to_llm_reg(family, False))
                else:
                    ret.append({"model_name": family.model_name, "is_builtin": False})

            ret.sort(key=sort_helper)
            return ret
        elif model_type == "embedding":
            from ..model.embedding import BUILTIN_EMBEDDING_MODELS
            from ..model.embedding.custom import get_user_defined_embeddings

            for model_name, family in BUILTIN_EMBEDDING_MODELS.items():
                if detailed:
                    ret.append(
                        await self._to_embedding_model_reg(family, is_builtin=True)
                    )
                else:
                    ret.append({"model_name": model_name, "is_builtin": True})

            for model_spec in get_user_defined_embeddings():
                if detailed:
                    ret.append(
                        await self._to_embedding_model_reg(model_spec, is_builtin=False)
                    )
                else:
                    ret.append(
                        {"model_name": model_spec.model_name, "is_builtin": False}
                    )

            ret.sort(key=sort_helper)
            return ret
        elif model_type == "image":
            from ..model.image import BUILTIN_IMAGE_MODELS
            from ..model.image.custom import get_user_defined_images

            for model_name, families in BUILTIN_IMAGE_MODELS.items():
                if detailed:
                    family = [x for x in families if x.model_hub == "huggingface"][0]
                    ret.append(await self._to_image_model_reg(family, is_builtin=True))
                else:
                    ret.append({"model_name": model_name, "is_builtin": True})

            for model_spec in get_user_defined_images():
                if detailed:
                    ret.append(
                        await self._to_image_model_reg(model_spec, is_builtin=False)
                    )
                else:
                    ret.append(
                        {"model_name": model_spec.model_name, "is_builtin": False}
                    )

            ret.sort(key=sort_helper)
            return ret
        elif model_type == "audio":
            from ..model.audio import BUILTIN_AUDIO_MODELS
            from ..model.audio.custom import get_user_defined_audios

            for model_name, families in BUILTIN_AUDIO_MODELS.items():
                if detailed:
                    family = [x for x in families if x.model_hub == "huggingface"][0]
                    ret.append(await self._to_audio_model_reg(family, is_builtin=True))
                else:
                    ret.append({"model_name": model_name, "is_builtin": True})

            for model_spec in get_user_defined_audios():
                if detailed:
                    ret.append(
                        await self._to_audio_model_reg(model_spec, is_builtin=False)
                    )
                else:
                    ret.append(
                        {"model_name": model_spec.model_name, "is_builtin": False}
                    )

            ret.sort(key=sort_helper)
            return ret
        elif model_type == "video":
            from ..model.video import BUILTIN_VIDEO_MODELS

            for model_name, families in BUILTIN_VIDEO_MODELS.items():
                if detailed:
                    family = [x for x in families if x.model_hub == "huggingface"][0]
                    ret.append(await self._to_video_model_reg(family, is_builtin=True))
                else:
                    ret.append({"model_name": model_name, "is_builtin": True})

            ret.sort(key=sort_helper)
            return ret
        elif model_type == "rerank":
            from ..model.rerank import BUILTIN_RERANK_MODELS
            from ..model.rerank.custom import get_user_defined_reranks

            for model_name, families in BUILTIN_RERANK_MODELS.items():
                if detailed:
                    family = [x for x in families if x.model_hub == "huggingface"][0]
                    ret.append(await self._to_rerank_model_reg(family, is_builtin=True))
                else:
                    ret.append({"model_name": model_name, "is_builtin": True})

            for model_spec in get_user_defined_reranks():
                if detailed:
                    ret.append(
                        await self._to_rerank_model_reg(model_spec, is_builtin=False)
                    )
                else:
                    ret.append(
                        {"model_name": model_spec.model_name, "is_builtin": False}
                    )

            ret.sort(key=sort_helper)
            return ret
        elif model_type == "flexible":
            from ..model.flexible import get_flexible_models

            ret = []

            for model_spec in get_flexible_models():
                if detailed:
                    ret.append(
                        await self._to_flexible_model_reg(model_spec, is_builtin=False)
                    )
                else:
                    ret.append(
                        {"model_name": model_spec.model_name, "is_builtin": False}
                    )

            ret.sort(key=sort_helper)
            return ret
        else:
            raise ValueError(f"Unsupported model type: {model_type}")

    @log_sync(logger=logger)
    async def get_model_registration(self, model_type: str, model_name: str) -> Any:
        # search in worker first
        if not self.is_local_deployment():
            workers = list(self._worker_address_to_worker.values())
            for worker in workers:
                f = await worker.get_model_registration(model_type, model_name)
                if f is not None:
                    return f

        if model_type == "LLM":
            from ..model.llm import BUILTIN_LLM_FAMILIES, get_user_defined_llm_families

            for f in BUILTIN_LLM_FAMILIES + get_user_defined_llm_families():
                if f.model_name == model_name:
                    return f

            raise ValueError(f"Model {model_name} not found")
        elif model_type == "embedding":
            from ..model.embedding import BUILTIN_EMBEDDING_MODELS
            from ..model.embedding.custom import get_user_defined_embeddings

            for f in (
                list(BUILTIN_EMBEDDING_MODELS.values()) + get_user_defined_embeddings()
            ):
                if f.model_name == model_name:
                    return f
            raise ValueError(f"Model {model_name} not found")
        elif model_type == "image":
            from ..model.image import BUILTIN_IMAGE_MODELS
            from ..model.image.custom import get_user_defined_images

            if model_name in BUILTIN_IMAGE_MODELS:
                return [
                    x
                    for x in BUILTIN_IMAGE_MODELS[model_name]
                    if x.model_hub == "huggingface"
                ][0]
            else:
                for f in get_user_defined_images():
                    if f.model_name == model_name:
                        return f
            raise ValueError(f"Model {model_name} not found")
        elif model_type == "audio":
            from ..model.audio import BUILTIN_AUDIO_MODELS
            from ..model.audio.custom import get_user_defined_audios

            if model_name in BUILTIN_AUDIO_MODELS:
                return [
                    x
                    for x in BUILTIN_AUDIO_MODELS[model_name]
                    if x.model_hub == "huggingface"
                ][0]
            else:
                for f in get_user_defined_audios():
                    if f.model_name == model_name:
                        return f
            raise ValueError(f"Model {model_name} not found")
        elif model_type == "rerank":
            from ..model.rerank import BUILTIN_RERANK_MODELS
            from ..model.rerank.custom import get_user_defined_reranks

            if model_name in BUILTIN_RERANK_MODELS:
                return [
                    x
                    for x in BUILTIN_RERANK_MODELS[model_name]
                    if x.model_hub == "huggingface"
                ][0]
            else:
                for f in get_user_defined_reranks():
                    if f.model_name == model_name:
                        return f
            raise ValueError(f"Model {model_name} not found")
        elif model_type == "flexible":
            from ..model.flexible import get_flexible_models

            for f in get_flexible_models():
                if f.model_name == model_name:
                    return f
            raise ValueError(f"Model {model_name} not found")
        else:
            raise ValueError(f"Unsupported model type: {model_type}")

    @log_async(logger=logger)
    async def query_engines_by_model_name(
        self, model_name: str, model_type: Optional[str] = None
    ):
        # search in worker first
        workers = list(self._worker_address_to_worker.values())
        for worker in workers:
            res = await worker.query_engines_by_model_name(
                model_name, model_type=model_type
            )
            if res is not None:
                return res

        return get_engine_params_by_name(model_type, model_name)

    @log_async(logger=logger)
    async def register_model(
        self,
        model_type: str,
        model: str,
        persist: bool,
        worker_ip: Optional[str] = None,
    ):
        if model_type in self._custom_register_type_to_cls:
            (
                model_spec_cls,
                register_fn,
                unregister_fn,
                generate_fn,
            ) = self._custom_register_type_to_cls[model_type]

            target_ip_worker_ref = (
                self._get_worker_ref_by_ip(worker_ip) if worker_ip is not None else None
            )
            if (
                worker_ip is not None
                and not self.is_local_deployment()
                and target_ip_worker_ref is None
            ):
                raise ValueError(
                    f"Worker ip address {worker_ip} is not in the cluster."
                )

            if target_ip_worker_ref:
                await target_ip_worker_ref.register_model(model_type, model, persist)
                return

            model_spec = model_spec_cls.parse_raw(model)
            try:
                register_fn(model_spec, persist)
                await self._cache_tracker_ref.record_model_version(
                    generate_fn(model_spec), self.address
                )
            except ValueError as e:
                raise e
            except Exception as e:
                unregister_fn(model_spec.model_name, raise_error=False)
                raise e
        else:
            raise ValueError(f"Unsupported model type: {model_type}")

    @log_async(logger=logger)
    async def unregister_model(self, model_type: str, model_name: str):
        if model_type in self._custom_register_type_to_cls:
            _, _, unregister_fn, _ = self._custom_register_type_to_cls[model_type]
            unregister_fn(model_name, False)

            if not self.is_local_deployment():
                workers = list(self._worker_address_to_worker.values())
                for worker in workers:
                    await worker.unregister_model(model_type, model_name)

            await self._cache_tracker_ref.unregister_model_version(model_name)
        else:
            raise ValueError(f"Unsupported model type: {model_type}")

    def _gen_model_uid(self, model_name: str) -> str:
        if model_name not in self._model_uid_to_replica_info:
            return model_name
        logger.debug(
            f"{model_name} exists in xinference. Generate suffix to {model_name} for model_uid."
        )
        return f"{model_name}-{gen_random_string(8)}"

    async def get_model_versions(self, model_type: str, model_name: str) -> List[Dict]:
        return await self._cache_tracker_ref.get_model_versions(model_name)

    async def get_model_version_count(self, model_name: str) -> int:
        return await self._cache_tracker_ref.get_model_version_count(model_name)

    @log_async(logger=logger)
    async def launch_model_by_version(
        self,
        model_uid: Optional[str],
        model_type: str,
        model_engine: Optional[str],
        model_version: str,
        replica: int = 1,
        n_gpu: Optional[Union[int, str]] = "auto",
        wait_ready: bool = True,
    ):
        parse_results = parse_model_version(model_version, model_type)

        if model_type == "image" and len(parse_results) == 2:
            kwargs = {"controlnet": parse_results[1]}
        else:
            kwargs = {}

        return await self.launch_builtin_model(
            model_uid=model_uid,
            model_name=parse_results[0],
            model_engine=model_engine,
            model_size_in_billions=parse_results[1] if model_type == "LLM" else None,
            model_format=parse_results[2] if model_type == "LLM" else None,
            quantization=parse_results[3] if model_type == "LLM" else None,
            model_type=model_type,
            replica=replica,
            n_gpu=n_gpu,
            wait_ready=wait_ready,
            model_version=model_version,
            **kwargs,
        )

    async def launch_builtin_model(
        self,
        model_uid: Optional[str],
        model_name: str,
        model_size_in_billions: Optional[Union[int, str]],
        model_format: Optional[str],
        quantization: Optional[str],
        model_engine: Optional[str],
        model_type: Optional[str],
        replica: int = 1,
        n_gpu: Optional[Union[int, str]] = "auto",
        n_worker: Optional[int] = 1,
        request_limits: Optional[int] = None,
        wait_ready: bool = True,
        model_version: Optional[str] = None,
        peft_model_config: Optional[PeftModelConfig] = None,
        worker_ip: Optional[str] = None,
        gpu_idx: Optional[Union[int, List[int]]] = None,
        download_hub: Optional[Literal["huggingface", "modelscope", "csghub"]] = None,
        model_path: Optional[str] = None,
        **kwargs,
    ) -> str:
        if self.is_local_deployment() and n_worker > 1:  # type: ignore
            # ignore n_worker > 1 if local deployment
            logger.warning("Local deployment, ignore n_worker(%s)", n_worker)
            n_worker = 1

        if n_worker > 1:  # type: ignore
            # distributed inference
            return await self._launch_builtin_sharded_model(
                model_uid,
                model_name,
                model_size_in_billions,
                model_format,
                quantization,
                model_engine,
                model_type,
                replica=replica,
                n_gpu=n_gpu,
                n_worker=n_worker,
                request_limits=request_limits,
                wait_ready=wait_ready,
                model_version=model_version,
                peft_model_config=peft_model_config,
                worker_ip=worker_ip,
                gpu_idx=gpu_idx,
                download_hub=download_hub,
                model_path=model_path,
                **kwargs,
            )

        # search in worker first
        if not self.is_local_deployment():
            workers = list(self._worker_address_to_worker.values())
            for worker in workers:
                res = await worker.get_model_registration(model_type, model_name)
                if res is not None:
                    worker_ip = worker.address.split(":")[0]

        target_ip_worker_ref = (
            self._get_worker_ref_by_ip(worker_ip) if worker_ip is not None else None
        )
        if (
            worker_ip is not None
            and not self.is_local_deployment()
            and target_ip_worker_ref is None
        ):
            raise ValueError(f"Worker ip address {worker_ip} is not in the cluster.")
        if worker_ip is not None and self.is_local_deployment():
            logger.warning(
                f"You specified the worker ip: {worker_ip} in local mode, "
                f"xinference will ignore this option."
            )

        if kwargs.get("enable_tensorizer", None) and (
            (
                model_engine is None
                or model_engine.lower() != "transformers"
                or model_format != "pytorch"
                or quantization != "none"
                or model_type != "LLM"
            )
        ):
            raise ValueError(
                "Tensorizer can only be enabled for LLM models with Transformers engine, PyTorch format, and none quantization."
            )

        if kwargs.get("enable_tensorizer", None) and model_name in [
            "OmniLMM",
            "yi-vl-chat",
            "deepseek-vl-chat",
        ]:
            raise ValueError("Tensorizer is not supported for %s." % model_name)

        if model_uid is None:
            model_uid = self._gen_model_uid(model_name)

        # Xavier-related
        enable_xavier: bool = (
            bool(kwargs.pop("enable_xavier", False))
            and model_engine is not None
            and model_engine.lower() == "vllm"
        )
        store_address = None
        store_port = None
        world_size = None
        if enable_xavier:
            if replica <= 1:
                logger.warning(f"Enabling xavier when `replica<=1` is meaningless.")
                enable_xavier = False
            else:
                from ..model.llm.vllm.xavier.block_tracker import VLLMBlockTracker
                from ..model.llm.vllm.xavier.collective_manager import CollectiveManager

                self._block_tracker_mapping[model_uid] = await xo.create_actor(
                    VLLMBlockTracker,
                    address=self.address,
                    uid=f"{VLLMBlockTracker.default_uid()}-{model_uid}",
                )
                world_size = replica + 1
                logger.info(f"Going to start xavier with world size: {world_size}")
                self._collective_manager_mapping[model_uid] = await xo.create_actor(
                    CollectiveManager,
                    address=self.address,
                    uid=f"{CollectiveManager.default_uid()}-{model_uid}",
                    model_uid=model_uid,
                )
                logger.info(f"Start collective manager for {model_uid} done.")

        model_size = str(model_size_in_billions) if model_size_in_billions else ""
        logger.debug(
            f"Enter launch_builtin_model, model_uid: {model_uid}, model_name: {model_name}, model_size: {model_size}, "
            f"model_format: {model_format}, quantization: {quantization}, replica: {replica}, enable_xavier: {enable_xavier}, "
            f"kwargs: {kwargs}"
        )

        async def _launch_one_model(worker_ref, _replica_model_uid, rank: int):
            if _replica_model_uid in self._replica_model_uid_to_worker:
                raise ValueError(
                    f"Model is already in the model list, uid: {_replica_model_uid}"
                )

            nonlocal store_address
            nonlocal store_port
            xavier_config = (
                {
                    "block_tracker_uid": self._block_tracker_mapping[model_uid].uid,
                    "block_tracker_address": self._block_tracker_mapping[
                        model_uid
                    ].address,
                    "rank": rank,
                    "world_size": world_size,
                    "store_address": store_address,
                    "store_port": store_port,
                }
                if enable_xavier
                else None
            )

            if enable_xavier and rank == 0:
                rank0_address, _port = await worker_ref.launch_rank0_model(
                    _replica_model_uid, xavier_config
                )
                self._replica_model_uid_to_worker[_replica_model_uid] = worker_ref
                store_address = rank0_address.split(":")[0]
                store_port = _port
                return rank0_address

            replica_gpu_idx = assign_replica_gpu(_replica_model_uid, replica, gpu_idx)
            nonlocal model_type

            # LLM as default for compatibility
            model_type = model_type or "LLM"
            subpool_address = await worker_ref.launch_builtin_model(
                model_uid=_replica_model_uid,
                model_name=model_name,
                model_size_in_billions=model_size_in_billions,
                model_format=model_format,
                quantization=quantization,
                model_engine=model_engine,
                model_type=model_type,
                n_gpu=n_gpu,
                request_limits=request_limits,
                peft_model_config=peft_model_config,
                gpu_idx=replica_gpu_idx,
                download_hub=download_hub,
                model_path=model_path,
                xavier_config=xavier_config,
                **kwargs,
            )
            self._replica_model_uid_to_worker[_replica_model_uid] = worker_ref
            await worker_ref.wait_for_load(_replica_model_uid)
            return subpool_address

        async def _launch_model():
            try:
                worker_refs = []
                rank_addresses = []
                for _idx, rep_model_uid in enumerate(
                    iter_replica_model_uid(model_uid, replica)
                ):
                    worker_ref = (
                        target_ip_worker_ref
                        if target_ip_worker_ref is not None
                        else await self._choose_worker()
                    )
                    self._model_uid_to_replica_info[model_uid].replica_to_worker_refs[
                        _idx
                    ].append(worker_ref)
                    if enable_xavier and _idx == 0:
                        """
                        Start the rank 0 model actor on the worker that holds the rank 1 replica,
                        solely for constructing the collective communication world.
                        """
                        _uid = model_uid + "-rank0"
                        rank0_address = await _launch_one_model(worker_ref, _uid, 0)
                        worker_refs.append((worker_ref, _uid))
                        rank_addresses.append(rank0_address)

                    subpool_address = await _launch_one_model(
                        worker_ref, rep_model_uid, _idx + 1
                    )
                    worker_refs.append((worker_ref, rep_model_uid))
                    rank_addresses.append(subpool_address)

                # For xavier, start all the vllm instances first,
                # and then start the transfer component,
                # because the transfer actor needs all the rank addresses used for collective communication
                if enable_xavier:
                    logger.debug(f"Init transfer component for xavier...")
                    collective_manager_ref = self._collective_manager_mapping[model_uid]
                    tasks = []
                    for worker_ref, rep_model_uid in worker_refs:
                        tasks.append(
                            worker_ref.start_transfer_for_vllm(
                                rep_model_uid, rank_addresses
                            )
                        )
                    # Here you must use asyncio.gather, not a for loop,
                    # or you will get stuck.
                    await asyncio.gather(*tasks)

                    # init collective_manager
                    for idx, addr in enumerate(rank_addresses):
                        await collective_manager_ref.register_rank(
                            idx, addr, update=False
                        )

                    logger.debug(f"Init transfer component for xavier done.")
            except Exception:
                # terminate_model will remove the replica info.
                await self.terminate_model(model_uid, suppress_exception=True)
                await self._status_guard_ref.update_instance_info(
                    model_uid, {"status": LaunchStatus.ERROR.name}
                )
                raise

        if not is_valid_model_uid(model_uid):
            raise ValueError(
                "The model UID is invalid. Please specify the model UID by 0 < length <= 100."
            )

        if request_limits is not None and request_limits < 0:
            raise ValueError(
                "The `request_limits` parameter must be greater or equal than 0."
            )

        if model_uid in self._model_uid_to_replica_info:
            raise ValueError(f"Model is already in the model list, uid: {model_uid}")
        # Set replica info first for exception handler to terminate model.
        self._model_uid_to_replica_info[model_uid] = ReplicaInfo(
            replica=replica, scheduler=itertools.cycle(range(replica))
        )
        instance_info = InstanceInfo(
            model_name=model_name,
            model_uid=model_uid,
            model_version=model_version,
            model_ability=[],
            replica=replica,
            status=LaunchStatus.CREATING.name,
            instance_created_ts=int(time.time()),
        )
        await self._status_guard_ref.set_instance_info(model_uid, instance_info)
        if wait_ready:
            await _launch_model()
        else:
            task = asyncio.create_task(_launch_model())
            ASYNC_LAUNCH_TASKS[model_uid] = task
            task.add_done_callback(lambda _: callback_for_async_launch(model_uid))  # type: ignore
        return model_uid

    async def _launch_builtin_sharded_model(
        self,
        model_uid: Optional[str],
        model_name: str,
        model_size_in_billions: Optional[Union[int, str]],
        model_format: Optional[str],
        quantization: Optional[str],
        model_engine: Optional[str],
        model_type: Optional[str],
        replica: int = 1,
        n_gpu: Optional[Union[int, str]] = "auto",
        n_worker: Optional[int] = 1,
        request_limits: Optional[int] = None,
        wait_ready: bool = True,
        model_version: Optional[str] = None,
        peft_model_config: Optional[PeftModelConfig] = None,
        worker_ip: Optional[str] = None,
        gpu_idx: Optional[Union[int, List[int]]] = None,
        download_hub: Optional[Literal["huggingface", "modelscope", "csghub"]] = None,
        model_path: Optional[str] = None,
        **kwargs,
    ):
        available_workers = []
        # search workers if registered
        tasks = []
        if not worker_ip:
            all_workers = list(self._worker_address_to_worker)
            for worker in all_workers:
                tasks.append(
                    self._worker_address_to_worker[worker].get_model_registration(
                        model_type, model_name
                    )
                )
            res = await asyncio.gather(*tasks)
            for worker, res in zip(all_workers, res):
                # check regi
                if res:
                    available_workers.append(worker)
            if not available_workers:
                # no registration, use all workers
                available_workers = all_workers
        else:
            if isinstance(worker_ip, list):
                available_workers.extend(worker_ip)
            else:
                available_workers.append(worker_ip)

        async def _launch_model():
            # Validation of n_worker, intercept if it is greater than the available workers.
            if n_worker > len(available_workers):
                raise ValueError(
                    "n_worker cannot be larger than the number of available workers."
                )
            try:
                for _idx, rep_model_uid in enumerate(
                    iter_replica_model_uid(model_uid, replica)
                ):
                    replica_gpu_idx = assign_replica_gpu(
                        rep_model_uid, replica, gpu_idx
                    )
                    # launch shard
                    worker_refs = []
                    driver_info = None
                    for i_worker in range(n_worker):
                        worker_ref = await self._choose_worker(available_workers)
                        self._model_uid_to_replica_info[
                            model_uid
                        ].replica_to_worker_refs[_idx].append(worker_ref)
                        nonlocal model_type
                        model_type = model_type or "LLM"
                        if i_worker > 1:
                            assert (
                                driver_info is not None
                            ), "driver info should be passed by first model shard"
                        info = await worker_ref.launch_builtin_model(
                            model_uid=rep_model_uid,
                            model_name=model_name,
                            model_size_in_billions=model_size_in_billions,
                            model_format=model_format,
                            quantization=quantization,
                            model_engine=model_engine,
                            model_type=model_type,
                            n_gpu=n_gpu,
                            request_limits=request_limits,
                            peft_model_config=peft_model_config,
                            gpu_idx=replica_gpu_idx,
                            download_hub=download_hub,
                            model_path=model_path,
                            shard=i_worker,
                            n_worker=n_worker,
                            driver_info=driver_info,
                            **kwargs,
                        )
                        if i_worker == 0:
                            # info will be subpool address + driver info
                            # for shard 0
                            driver_info = info[1]
                        worker_refs.append(worker_ref)
                    self._replica_model_uid_to_worker[rep_model_uid] = worker_refs

                    # for distributed inference,
                    # launch will run asynchronously,
                    # wait for load complete
                    for worker_ref in worker_refs:
                        await worker_ref.wait_for_load(rep_model_uid)
            except:
                # terminate_model will remove the replica info.
                await self.terminate_model(model_uid, suppress_exception=True)
                await self._status_guard_ref.update_instance_info(
                    model_uid, {"status": LaunchStatus.ERROR.name}
                )
                raise

        if model_uid is None:
            model_uid = self._gen_model_uid(model_name)

        if not is_valid_model_uid(model_uid):
            raise ValueError(
                "The model UID is invalid. Please specify the model UID by 0 < length <= 100."
            )

        if request_limits is not None and request_limits < 0:
            raise ValueError(
                "The `request_limits` parameter must be greater or equal than 0."
            )

        if model_uid in self._model_uid_to_replica_info:
            raise ValueError(f"Model is already in the model list, uid: {model_uid}")

        # Set replica info first for exception handler to terminate model.
        self._model_uid_to_replica_info[model_uid] = ReplicaInfo(
            replica=replica, scheduler=itertools.cycle(range(replica))
        )
        instance_info = InstanceInfo(
            model_name=model_name,
            model_uid=model_uid,
            model_version=model_version,
            model_ability=[],
            replica=replica,
            n_worker=n_worker,
            status=LaunchStatus.CREATING.name,
            instance_created_ts=int(time.time()),
        )
        await self._status_guard_ref.set_instance_info(model_uid, instance_info)
        if wait_ready:
            await _launch_model()
        else:
            task = asyncio.create_task(_launch_model())
            ASYNC_LAUNCH_TASKS[model_uid] = task
            task.add_done_callback(lambda _: callback_for_async_launch(model_uid))  # type: ignore
        return model_uid

    async def get_launch_builtin_model_progress(self, model_uid: str) -> float:
        try:
            info = self._model_uid_to_replica_info[model_uid]
        except KeyError:
            # Not launched perhaps, just return 0.0 to prevent error
            return 0.0

        all_progress = 0.0
        i = 0
        for rep_model_uid in iter_replica_model_uid(model_uid, info.replica):
            request_id = f"launching-{rep_model_uid}"
            try:
                all_progress += await self._progress_tracker.get_progress(request_id)
                i += 1
            except KeyError:
                continue

        return all_progress / i if i > 0 else 0.0

    async def cancel_launch_builtin_model(self, model_uid: str):
        try:
            info = self._model_uid_to_replica_info[model_uid]
        except KeyError:
            raise RuntimeError(f"Model {model_uid} has not been launched yet")

        coros = []
        for i, rep_model_uid in enumerate(
            iter_replica_model_uid(model_uid, info.replica)
        ):
            worker_refs = self._model_uid_to_replica_info[
                model_uid
            ].replica_to_worker_refs[i]
            for worker_ref in worker_refs:
                coros.append(worker_ref.cancel_launch_model(rep_model_uid))
        try:
            await asyncio.gather(*coros)
        except RuntimeError:
            # some may have finished
            pass
        # remove replica info
        self._model_uid_to_replica_info.pop(model_uid, None)

    async def get_instance_info(
        self, model_name: Optional[str], model_uid: Optional[str]
    ) -> List[Dict]:
        infos = await self._status_guard_ref.get_instance_info(
            model_name=model_name, model_uid=model_uid
        )
        return [info.dict() for info in sorted(infos, key=lambda info: info.model_uid)]

    async def get_instance_count(self, model_name: str) -> int:
        return await self._status_guard_ref.get_instance_count(model_name)

    async def _check_dead_nodes(self):
        while True:
            try:
                dead_nodes = []
                for address, status in self._worker_status.items():
                    if (
                        time.time() - status.update_time
                        > XINFERENCE_HEALTH_CHECK_TIMEOUT
                    ):
                        status.failure_remaining_count -= 1
                    else:
                        status.failure_remaining_count = (
                            XINFERENCE_HEALTH_CHECK_FAILURE_THRESHOLD
                        )

                    if status.failure_remaining_count <= 0:
                        dead_models = []
                        for model_uid in self._replica_model_uid_to_worker:
                            worker_refs = self._replica_model_uid_to_worker[model_uid]
                            if not isinstance(worker_refs, list):
                                worker_refs = [worker_refs]
                            for worker_ref in worker_refs:
                                model_address = worker_ref.address
                                if model_address == address:
                                    dead_models.append(model_uid)
                        logger.error(
                            "Worker dead. address: %s, influenced models: %s",
                            address,
                            dead_models,
                        )
                        for replica_model_uid in dead_models:
                            model_uid, _ = parse_replica_model_uid(replica_model_uid)
                            self._model_uid_to_replica_info.pop(model_uid, None)
                            self._replica_model_uid_to_worker.pop(
                                replica_model_uid, None
                            )
                        dead_nodes.append(address)
                    elif (
                        status.failure_remaining_count
                        != XINFERENCE_HEALTH_CHECK_FAILURE_THRESHOLD
                    ):
                        logger.error(
                            "Worker timeout. address: %s, check count remaining %s...",
                            address,
                            status.failure_remaining_count,
                        )

                for address in dead_nodes:
                    self._worker_status.pop(address, None)
                    self._worker_address_to_worker.pop(address, None)
            finally:
                await asyncio.sleep(XINFERENCE_HEALTH_CHECK_INTERVAL)

    @log_async(logger=logger)
    async def terminate_model(self, model_uid: str, suppress_exception=False):
        async def _terminate_one_model(_replica_model_uid):
            worker_refs = self._replica_model_uid_to_worker.get(
                _replica_model_uid, None
            )
            if not isinstance(worker_refs, list):
                worker_refs = [worker_refs]

            for worker_ref in worker_refs:
                if worker_ref is None:
                    raise ValueError(
                        f"Model not found in the model list, uid: {_replica_model_uid}"
                    )
                await worker_ref.terminate_model(model_uid=_replica_model_uid)
            del self._replica_model_uid_to_worker[_replica_model_uid]

        replica_info = self._model_uid_to_replica_info.get(model_uid, None)
        if replica_info is None:
            raise ValueError(f"Model not found in the model list, uid: {model_uid}")

        for rep_model_uid in iter_replica_model_uid(model_uid, replica_info.replica):
            try:
                await _terminate_one_model(rep_model_uid)
            except Exception:
                if not suppress_exception:
                    raise
        self._model_uid_to_replica_info.pop(model_uid, None)

        # clear for xavier
        rank0_uid = model_uid + "-rank0"
        if rank0_uid in self._replica_model_uid_to_worker:
            await _terminate_one_model(rank0_uid)

        collective_manager_ref = self._collective_manager_mapping.pop(model_uid, None)
        if collective_manager_ref is not None:
            try:
                await xo.destroy_actor(collective_manager_ref)
            except Exception as e:
                logger.debug(
                    "Destroy collective_manager_ref failed, model uid: %s, error: %s",
                    model_uid,
                    e,
                )
            finally:
                logger.debug(
                    f"Destroy collective_manager_ref done. model uid: {model_uid}"
                )
        block_tracker_ref = self._block_tracker_mapping.pop(model_uid, None)
        if block_tracker_ref is not None:
            try:
                await xo.destroy_actor(block_tracker_ref)
            except Exception as e:
                logger.debug(
                    "Destroy block_tracker_ref failed, model uid: %s, error: %s",
                    model_uid,
                    e,
                )
            finally:
                logger.debug(f"Destroy block_tracker_ref done. model uid: {model_uid}")

    @log_async(logger=logger)
    async def get_model(self, model_uid: str) -> xo.ActorRefType["ModelActor"]:
        replica_info = self._model_uid_to_replica_info.get(model_uid, None)
        if replica_info is None:
            raise ValueError(f"Model not found in the model list, uid: {model_uid}")

        replica_model_uid = build_replica_model_uid(
            model_uid, next(replica_info.scheduler)
        )

        worker_ref = self._replica_model_uid_to_worker.get(replica_model_uid, None)
        if worker_ref is None:
            raise ValueError(
                f"Model not found in the model list, uid: {replica_model_uid}"
            )
        if isinstance(worker_ref, list):
            # get first worker to fetch information if model across workers
            worker_ref = worker_ref[0]
        return await worker_ref.get_model(model_uid=replica_model_uid)

    @log_async(logger=logger)
    async def get_model_status(self, replica_model_uid: str):
        worker_ref = self._replica_model_uid_to_worker.get(replica_model_uid, None)
        if worker_ref is None:
            raise ValueError(
                f"Model not found in the model list, uid: {replica_model_uid}"
            )
        if isinstance(worker_ref, list):
            # get status from first shard if model has multiple shards across workers
            worker_ref = worker_ref[0]
        return await worker_ref.get_model_status(replica_model_uid)

    @log_async(logger=logger)
    async def describe_model(self, model_uid: str) -> Dict[str, Any]:
        replica_info = self._model_uid_to_replica_info.get(model_uid, None)
        if replica_info is None:
            raise ValueError(f"Model not found in the model list, uid: {model_uid}")
        # Use rep id 0 to instead of next(replica_info.scheduler) to avoid
        # consuming the generator.
        replica_model_uid = build_replica_model_uid(model_uid, 0)
        worker_ref = self._replica_model_uid_to_worker.get(replica_model_uid, None)
        if worker_ref is None:
            raise ValueError(
                f"Model not found in the model list, uid: {replica_model_uid}"
            )
        if isinstance(worker_ref, list):
            # get status from first shard if model has multiple shards across workers
            worker_ref = worker_ref[0]
        info = await worker_ref.describe_model(model_uid=replica_model_uid)
        info["replica"] = replica_info.replica
        return info

    @log_async(logger=logger)
    async def list_models(self) -> Dict[str, Dict[str, Any]]:
        ret = {}

        workers = list(self._worker_address_to_worker.values())
        for worker in workers:
            ret.update(await worker.list_models())
        running_model_info = {parse_replica_model_uid(k)[0]: v for k, v in ret.items()}
        # add replica count
        for k, v in running_model_info.items():
            v["replica"] = self._model_uid_to_replica_info[k].replica
        return running_model_info

    def is_local_deployment(self) -> bool:
        # TODO: temporary.
        return (
            len(self._worker_address_to_worker) == 1
            and list(self._worker_address_to_worker)[0] == self.address
        )

    @log_async(logger=logger)
    async def list_cached_models(
        self, model_name: Optional[str] = None, worker_ip: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        target_ip_worker_ref = (
            self._get_worker_ref_by_ip(worker_ip) if worker_ip is not None else None
        )
        if (
            worker_ip is not None
            and not self.is_local_deployment()
            and target_ip_worker_ref is None
        ):
            raise ValueError(f"Worker ip address {worker_ip} is not in the cluster.")

        # search assigned worker and return
        if target_ip_worker_ref:
            cached_models = await target_ip_worker_ref.list_cached_models(model_name)
            cached_models = sorted(cached_models, key=lambda x: x["model_name"])
            return cached_models

        # search all worker
        cached_models = []
        for worker in self._worker_address_to_worker.values():
            res = await worker.list_cached_models(model_name)
            cached_models.extend(res)
        cached_models = sorted(cached_models, key=lambda x: x["model_name"])
        return cached_models

    @log_async(logger=logger)
    async def abort_request(
        self,
        model_uid: str,
        request_id: str,
        block_duration: int = XINFERENCE_DEFAULT_CANCEL_BLOCK_DURATION,
    ) -> Dict:
        from ..model.scheduler.core import AbortRequestMessage

        res = {"msg": AbortRequestMessage.NO_OP.name}
        replica_info = self._model_uid_to_replica_info.get(model_uid, None)
        if not replica_info:
            return res
        replica_cnt = replica_info.replica

        # Query all replicas
        for rep_mid in iter_replica_model_uid(model_uid, replica_cnt):
            worker_ref = self._replica_model_uid_to_worker.get(rep_mid, None)
            if worker_ref is None:
                continue
            if isinstance(worker_ref, list):
                # get status from first shard if model has multiple shards across workers
                worker_ref = worker_ref[0]
            model_ref = await worker_ref.get_model(model_uid=rep_mid)
            result_info = await model_ref.abort_request(request_id, block_duration)
            res["msg"] = result_info
            if result_info == AbortRequestMessage.DONE.name:
                break
            elif result_info == AbortRequestMessage.NOT_FOUND.name:
                logger.debug(f"Request id: {request_id} not found for model {rep_mid}")
            else:
                logger.debug(f"No-op for model {rep_mid}")
        return res

    @log_async(logger=logger)
    async def add_worker(self, worker_address: str):
        from .worker import WorkerActor

        assert (
            worker_address not in self._worker_address_to_worker
        ), f"Worker {worker_address} exists"

        worker_ref = await xo.actor_ref(
            address=worker_address, uid=WorkerActor.default_uid()
        )
        self._worker_address_to_worker[worker_address] = worker_ref
        logger.debug("Worker %s has been added successfully", worker_address)

    @log_async(logger=logger)
    async def remove_worker(self, worker_address: str):
        uids_to_remove = []
        for model_uid in self._replica_model_uid_to_worker:
            worker_refs = self._replica_model_uid_to_worker[model_uid]
            if not isinstance(worker_refs, list):
                worker_refs = [worker_refs]
            for worker_ref in worker_refs:
                model_address = worker_ref.address
                if isinstance(model_address, str) and model_address == worker_address:
                    uids_to_remove.append(model_uid)
                elif (
                    isinstance(model_address, list) and worker_address in model_address
                ):
                    uids_to_remove.append(model_uid)

        for replica_model_uid in uids_to_remove:
            model_uid, _ = parse_replica_model_uid(replica_model_uid)
            self._model_uid_to_replica_info.pop(model_uid, None)
            self._replica_model_uid_to_worker.pop(replica_model_uid, None)

        if worker_address in self._worker_address_to_worker:
            del self._worker_address_to_worker[worker_address]
            logger.debug("Worker %s has been removed successfully", worker_address)
        else:
            logger.warning(
                f"Worker {worker_address} cannot be removed since it is not registered to supervisor."
            )

    async def report_worker_status(
        self, worker_address: str, status: Dict[str, Union[ResourceStatus, GPUStatus]]
    ):
        if worker_address not in self._worker_status:
            logger.debug("Worker %s resources: %s", worker_address, status)
            self._worker_status[worker_address] = WorkerStatus(
                update_time=time.time(),
                failure_remaining_count=XINFERENCE_HEALTH_CHECK_FAILURE_THRESHOLD,
                status=status,
            )
        else:
            worker_status = self._worker_status[worker_address]
            worker_status.update_time = time.time()
            worker_status.status = status

    async def list_deletable_models(
        self, model_version: str, worker_ip: Optional[str] = None
    ) -> List[str]:
        target_ip_worker_ref = (
            self._get_worker_ref_by_ip(worker_ip) if worker_ip is not None else None
        )
        if (
            worker_ip is not None
            and not self.is_local_deployment()
            and target_ip_worker_ref is None
        ):
            raise ValueError(f"Worker ip address {worker_ip} is not in the cluster.")

        ret = []
        if target_ip_worker_ref:
            ret = await target_ip_worker_ref.list_deletable_models(
                model_version=model_version,
            )
            return ret

        for worker in self._worker_address_to_worker.values():
            path = await worker.list_deletable_models(model_version=model_version)
            ret.extend(path)
        return ret

    async def confirm_and_remove_model(
        self, model_version: str, worker_ip: Optional[str] = None
    ) -> bool:
        target_ip_worker_ref = (
            self._get_worker_ref_by_ip(worker_ip) if worker_ip is not None else None
        )
        if (
            worker_ip is not None
            and not self.is_local_deployment()
            and target_ip_worker_ref is None
        ):
            raise ValueError(f"Worker ip address {worker_ip} is not in the cluster.")

        if target_ip_worker_ref:
            ret = await target_ip_worker_ref.confirm_and_remove_model(
                model_version=model_version,
            )
            return ret
        ret = True
        for worker in self._worker_address_to_worker.values():
            ret = ret and await worker.confirm_and_remove_model(
                model_version=model_version,
            )
        return ret

    async def get_workers_info(self) -> List[Dict[str, Any]]:
        ret = []
        for worker in self._worker_address_to_worker.values():
            ret.append(await worker.get_workers_info())
        return ret

    async def get_supervisor_info(self) -> Dict[str, Any]:
        ret = {
            "supervisor_ip": self.address,
        }
        return ret

    async def trigger_exit(self) -> bool:
        try:
            os.kill(os.getpid(), signal.SIGTERM)
        except Exception as e:
            logger.info(f"trigger exit error: {e}")
            return False
        return True

    async def abort_cluster(self) -> bool:
        ret = True
        for worker in self._worker_address_to_worker.values():
            ret = ret and await worker.trigger_exit()

        ret = ret and await self.trigger_exit()
        return ret

    @staticmethod
    def record_metrics(name, op, kwargs):
        record_metrics(name, op, kwargs)

    async def get_progress(self, request_id: str) -> float:
        return await self._progress_tracker.get_progress(request_id)

    async def call_collective_manager(
        self, model_uid: str, func_name: str, *args, **kwargs
    ):
        """
        Used by worker.
        """
        collective_manager_ref = self._collective_manager_mapping[model_uid]
        await getattr(collective_manager_ref, func_name)(*args, **kwargs)
