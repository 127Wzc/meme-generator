import hashlib
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import json
import os
import pkgutil
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Optional, Union

from .config import meme_config
from .exception import NoSuchMeme
from .log import logger
from .meme import CommandShortcut, Meme, MemeArgsType, MemeFunction, MemeParamsType

_memes: dict[str, Meme] = {}
_source_roots: set[Path] = {Path(__file__).parent / "memes"}
_loading: ContextVar[Optional[dict[str, Meme]]] = ContextVar(
    "loading_memes", default=None
)


class _SourceLoader(importlib.machinery.SourceFileLoader):
    """Compile current source without reading or writing bytecode caches."""

    def get_code(self, fullname):
        filename = self.get_filename(fullname)
        return self.source_to_code(self.get_data(filename), filename)


class _ReloadFinder(importlib.abc.MetaPathFinder):
    def __init__(self, roots):
        self.roots = roots

    def find_spec(self, fullname, path=None, target=None):
        if _loading.get() is None:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if (
            spec
            and isinstance(spec.loader, importlib.machinery.SourceFileLoader)
            and spec.origin
            and any(root in Path(spec.origin).resolve().parents for root in self.roots)
        ):
            spec.loader = _SourceLoader(fullname, spec.origin)
            return spec
        return None


def get_meme_dirs() -> list[Path]:
    value = os.environ.get("MEME_DIRS")
    if value is None:
        return meme_config.meme.meme_dirs
    directories = json.loads(value or "[]")
    if not isinstance(directories, list) or not all(
        isinstance(directory, str) and directory.strip() for directory in directories
    ):
        raise ValueError("MEME_DIRS must be a JSON array of directory paths")
    return [Path(directory) for directory in directories]


def load_all_memes():
    if meme_config.meme.load_builtin_memes:
        for module in pkgutil.iter_modules([str(Path(__file__).parent / "memes")]):
            if not module.name.startswith("_"):
                load_meme(f"meme_generator.memes.{module.name}")
    for directory in get_meme_dirs():
        if directory.is_dir():
            load_memes(directory)


@contextmanager
def reload_memes():
    """Build a fresh registry; publish only after the caller's preparation succeeds."""
    global _memes
    roots = [*_source_roots, *get_meme_dirs()]
    roots = [root.resolve() for root in roots]

    def belongs(module):
        filename = getattr(module, "__file__", None)
        if not filename:
            return False
        parents = Path(filename).resolve().parents
        return any(root in parents for root in roots)

    previous = {
        name: module for name, module in list(sys.modules.items()) if belongs(module)
    }
    candidate: dict[str, Meme] = {}
    token = _loading.set(candidate)
    try:
        for name in previous:
            sys.modules.pop(name, None)
        importlib.invalidate_caches()
        finder = _ReloadFinder(roots)
        sys.meta_path.insert(0, finder)
        try:
            load_all_memes()
        finally:
            sys.meta_path.remove(finder)
        yield candidate
        _memes = candidate
    except BaseException:
        for name, module in list(sys.modules.items()):
            if belongs(module):
                sys.modules.pop(name, None)
        sys.modules.update(previous)
        raise
    finally:
        _loading.reset(token)


def path_to_module_name(path: Path) -> str:
    rel_path = path.resolve().relative_to(Path.cwd().resolve())
    if rel_path.stem == "__init__":
        return ".".join(rel_path.parts[:-1])
    else:
        return ".".join(rel_path.parts[:-1] + (rel_path.stem,))


def load_meme(module_path: Union[str, Path]):
    module_name = (
        path_to_module_name(module_path)
        if isinstance(module_path, Path)
        else module_path
    )
    try:
        importlib.import_module(module_name)
    except Exception as e:
        if _loading.get() is not None:
            raise
        logger.opt(colors=True, exception=e).error(f"Failed to import {module_path}!")


def load_memes(dir_path: Union[str, Path]):
    directory = Path(dir_path).resolve()
    if not directory.is_dir():
        return
    if not any(
        root == directory or root in directory.parents for root in _source_roots
    ):
        _source_roots.difference_update(
            root for root in list(_source_roots) if directory in root.parents
        )
        _source_roots.add(directory)
    dir_path = str(directory)
    # Keep external packages (and relative helper imports) out of public names.
    namespace = "_meme_external_" + hashlib.sha256(dir_path.encode()).hexdigest()
    if namespace not in sys.modules:
        package = ModuleType(namespace)
        package.__path__ = [dir_path]
        package.__package__ = namespace
        package.__file__ = str(directory / "__init__.py")
        package.__spec__ = importlib.util.spec_from_loader(
            namespace, loader=None, is_package=True
        )
        sys.modules[namespace] = package

    for module_info in pkgutil.iter_modules([dir_path]):
        if module_info.name.startswith("_"):
            continue
        if not (
            module_spec := module_info.module_finder.find_spec(module_info.name, None)
        ):
            continue
        if not (module_path := module_spec.origin):
            continue
        module_name = f"{namespace}.{module_info.name}"
        module_spec = importlib.util.spec_from_file_location(module_name, module_path)
        if not module_spec or not (module_loader := module_spec.loader):
            continue
        if isinstance(module_loader, importlib.machinery.SourceFileLoader):
            module_loader = _SourceLoader(module_name, module_path)
            module_spec.loader = module_loader
        try:
            module = importlib.util.module_from_spec(module_spec)
            sys.modules[module_name] = module
            module_loader.exec_module(module)
        except Exception as e:
            sys.modules.pop(module_name, None)
            if _loading.get() is not None:
                raise
            logger.opt(colors=True, exception=e).error(
                f"Failed to import {module_path}!"
            )

    # Repositories may group meme packages in directories without __init__.py.
    # Packages themselves own their imports; do not execute their helpers twice.
    directory = Path(dir_path)
    if directory.is_dir():
        for child in sorted(directory.iterdir()):
            if (
                child.is_dir()
                and not child.name.startswith(("_", "."))
                and not child.is_symlink()
                and not (child / "__init__.py").exists()
            ):
                load_memes(child)


def add_meme(
    key: str,
    function: MemeFunction,
    *,
    min_images: int = 0,
    max_images: int = 0,
    min_texts: int = 0,
    max_texts: int = 0,
    default_texts: list[str] = [],
    args_type: Optional[MemeArgsType] = None,
    keywords: list[str] = [],
    shortcuts: list[CommandShortcut] = [],
    tags: set[str] = set(),
    date_created: datetime = datetime(2021, 5, 4),
    date_modified: datetime = datetime.now(),
):
    registry = _loading.get()
    if registry is None:
        registry = _memes
    if key in registry:
        logger.warning(f'Meme with key "{key}" already exists!')
        return

    if key in meme_config.meme.meme_disabled_list:
        logger.warning(f'The key "{key}" is in the disabled list!')
        return

    meme = Meme(
        key,
        function,
        MemeParamsType(
            min_images, max_images, min_texts, max_texts, default_texts, args_type
        ),
        keywords=keywords,
        shortcuts=shortcuts,
        tags=tags,
        date_created=date_created,
        date_modified=date_modified,
    )

    registry[key] = meme


def get_meme(key: str) -> Meme:
    if key not in _memes:
        raise NoSuchMeme(key)
    return _memes[key]


def get_memes() -> list[Meme]:
    return list(_memes.values())


def get_meme_keys() -> list[str]:
    return list(_memes.keys())
