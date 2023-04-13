import logging
from pathlib import Path
from typing import Dict, Optional

from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _

from paasng.accessories.smart_advisor.models import cleanup_module, tag_module
from paasng.accessories.smart_advisor.tagging import dig_tags_local_repo
from paasng.dev_resources.sourcectl.controllers.package import PackageController
from paasng.dev_resources.sourcectl.exceptions import GetAppYamlError, GetProcfileError
from paasng.dev_resources.sourcectl.models import VersionInfo
from paasng.dev_resources.sourcectl.repo_controller import get_repo_controller
from paasng.engine.configurations.source_file import get_metadata_reader
from paasng.engine.exceptions import DeployShouldAbortError
from paasng.engine.models import Deployment, EngineApp
from paasng.engine.utils.output import DeployStream, Style
from paasng.extensions.declarative.handlers import DescriptionHandler, get_desc_handler
from paasng.extensions.declarative.models import DeploymentDescription
from paasng.extensions.smart_app.patcher import SourceCodePatcherWithDBDriver
from paasng.platform.modules.constants import SourceOrigin
from paasng.platform.modules.models import Module
from paasng.platform.modules.specs import ModuleSpecs
from paasng.utils.validators import validate_procfile

logger = logging.getLogger(__name__)


def get_processes(deployment: Deployment, stream: Optional[DeployStream] = None) -> Dict[str, str]:  # noqa: C901
    """Get the processes data from SourceCode
    1. Try to get processes data from DeploymentDescription at first.
    2. Try to get the process data from DeployConfig, which only work from Image Application
    3. Try to get the process data from Procfile

    if step (1) and step (3) get processes data both, will use the one got from step (3)

    :param Deployment deployment: 当前的部署对象
    :param DeployStream stream: 日志流对象, 用于记录日志
    :raises: DeployShouldAbortError
    """
    module = deployment.app_environment.module
    operator = deployment.operator
    version_info = deployment.version_info
    relative_source_dir = deployment.get_source_dir()

    proc_data = None
    if deployment:
        try:
            deploy_desc = DeploymentDescription.objects.get(deployment=deployment)
            proc_data = deploy_desc.get_procfile()
        except DeploymentDescription.DoesNotExist:
            logger.info("Can't get related DeploymentDescription, read Procfile directly.")

    if not proc_data:
        deploy_config = module.get_deploy_config()
        proc_data = deploy_config.procfile

    try:
        metadata_reader = get_metadata_reader(module, operator=operator, source_dir=relative_source_dir)
        proc_data_form_source = metadata_reader.get_procfile(version_info)
    except GetProcfileError as e:
        if not proc_data:
            raise DeployShouldAbortError(reason=f'Procfile error: {e.message}') from e
    except NotImplementedError:
        """对于不支持从源码读取进程信息的应用, 忽略异常, 因为可能在其他分支已成功获取到 proc_data"""
    else:
        if proc_data:
            logger.warning("Process definition conflict, will use the one defined in `Procfile`")
            if stream:
                stream.write_message(
                    Style.Warning(_("Warning: Process definition conflict, will use the one defined in `Procfile`"))
                )
        proc_data = proc_data_form_source

    if proc_data is None:
        raise DeployShouldAbortError(_("Missing process definition"))
    try:
        return validate_procfile(procfile=proc_data)
    except ValidationError as e:
        raise DeployShouldAbortError(e.message) from e


def get_source_dir(module: Module, operator: str, version_info: VersionInfo) -> str:
    """A helper to get source_dir.
    For package App, we should parse source_dir from Application Description File.
    Otherwise,  we must get source_dir from property of module.
    """
    # Note: 对于非源码包类型的应用, 只有产品上配置的部署目录会生效
    if not ModuleSpecs(module).deploy_via_package:
        return module.get_source_obj().get_source_dir()

    # Note: 对于源码包类型的应用, 部署目录需要从源码包根目录下的 app_desc.yaml 中读取
    handler = get_app_description_handler(module, operator, version_info)
    if handler is None:
        return ''
    return handler.get_deploy_desc(module.name).source_dir


_current_path = Path('.')


def get_app_description_handler(
    module: Module, operator: str, version_info: VersionInfo, source_dir: Path = _current_path
) -> Optional[DescriptionHandler]:
    """Get App Description handler from app.yaml/app_desc.yaml"""
    try:
        metadata_reader = get_metadata_reader(module, operator=operator, source_dir=source_dir)
    except NotImplementedError:
        return None
    try:
        app_desc = metadata_reader.get_app_desc(version_info)
    except GetAppYamlError:
        return None

    return get_desc_handler(app_desc)


def get_source_package_path(deployment: Deployment) -> str:
    """Return the blobstore path for storing source files package"""
    engine_app = deployment.get_engine_app()
    branch = deployment.source_version_name
    revision = deployment.source_revision

    slug_name = f'{engine_app.name}:{branch}:{revision}'
    return f'{engine_app.region}/home/{slug_name}/tar'


def download_source_to_dir(module: Module, operator: str, deployment: Deployment, working_path: Path):
    """Download and extract the module's source files to local path, will generate Procfile if necessary

    :param operator: current operator's user_id
    """
    spec = ModuleSpecs(module)
    if spec.source_origin_specs.source_origin in [SourceOrigin.AUTHORIZED_VCS, SourceOrigin.SCENE]:
        get_repo_controller(module, operator=operator).export(working_path, deployment.version_info)
    elif spec.deploy_via_package:
        PackageController.init_by_module(module, operator).export(working_path, deployment.version_info)
    else:
        raise NotImplementedError

    try:
        SourceCodePatcherWithDBDriver(module, working_path, deployment).add_procfile()
    except Exception:
        logger.exception("Unexpected exception occurred when injecting Procfile.")
        return


def check_source_package(engine_app: EngineApp, package_path: Path, stream: DeployStream):
    """Check module source package, produce warning infos"""
    # Check source package size
    warning_threshold = settings.ENGINE_APP_SOURCE_SIZE_WARNING_THRESHOLD_MB
    size = package_path.stat().st_size
    if size > warning_threshold * 1024 * 1024:
        stream.write_message(
            Style.Warning(
                _("WARNING: 应用源码包体积过大（>{warning_threshold}MB），将严重影响部署性能，请尝试清理不必要的文件来减小体积。").format(
                    warning_threshold=warning_threshold
                )
            )
        )
        logger.error(f"Engine app {engine_app.name}'s source is too big, size={size}")


def tag_module_from_source_files(module, source_files_path):
    """Dig and tag the module from application source files"""
    try:
        tags = dig_tags_local_repo(str(source_files_path))
        cleanup_module(module)

        logging.info(f"Tagging module[{module.pk}]: {tags}")
        tag_module(module, tags, source="source_analyze")
    except Exception:
        logger.exception("Unable to tagging module")