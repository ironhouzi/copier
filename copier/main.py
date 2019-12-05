import filecmp
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from . import vcs
from .config import make_config
from .config.objects import Flags
from .tools import (
    Style,
    Renderer,
    copy_file,
    get_jinja_renderer,
    get_name_filters,
    make_folder,
    printf,
    prompt_bool,
)
from .types import (
    AnyByStrDict,
    CheckPathFunc,
    OptBool,
    OptStr,
    OptStrSeq,
    PathSeq,
    StrOrPath,
    StrOrPathSeq,
    StrSeq,
)

__all__ = ("copy", "copy_local")

RE_TMPL = re.compile(r"\.tmpl$", re.IGNORECASE)


def copy(
    src_path: OptStr = None,
    dst_path: StrOrPath = ".",
    data: AnyByStrDict = None,
    *,
    exclude: OptStrSeq = None,
    include: OptStrSeq = None,
    skip_if_exists: OptStrSeq = None,
    tasks: OptStrSeq = None,
    envops: AnyByStrDict = None,
    extra_paths: OptStrSeq = None,
    pretend: OptBool = False,
    force: OptBool = False,
    skip: OptBool = False,
    quiet: OptBool = False,
    cleanup_on_error: OptBool = True,
) -> None:
    """
    Uses the template in src_path to generate a new project at dst_path.

    Arguments:

    - src_path (str):
        Absolute path to the project skeleton. May be a version control system URL.
        If `None`, it will be taken from `dst_path/.copier-answers.yml` or fail.

    - dst_path (str):
        Absolute path to where to render the skeleton

    - data (dict):
        Optional. Data to be passed to the templates in addtion to the user data from
        a `copier.json`.

    - exclude (list):
        A list of names or shell-style patterns matching files or folders
        that must not be copied.

    - include (list):
        A list of names or shell-style patterns matching files or folders that
        must be included, even if its name are in the `exclude` list.
        Eg: `['.gitignore']`. The default is an empty list.

    - skip_if_exists (list):
        Skip any of these files if another with the same name already exists in the
        destination folder. (it only makes sense if you are copying to a folder that
        already exists).

    - tasks (list):
        Optional lists of commands to run in order after finishing the copy.
        Like in the templates files, you can use variables on the commands that will
        be replaced by the real values before running the command.
        If one of the commands fail, the rest of them will not run.

    - envops (dict):
        Extra options for the Jinja template environment.

    - extra_paths (list):
        Optional. Additional paths, outside the `src_path`, from where to search for
        templates. This is intended to be used with shared parent templates, files
        with macros, etc. outside the copied project skeleton.

    - pretend (bool):
        Run but do not make any changes

    - force (bool):
        Overwrite files that already exist, without asking

    - skip (bool):
        Skip files that already exist, without asking

    - quiet (bool):
        Suppress the status output

    - cleanup_on_error (bool):
        Remove the destination folder if the copy process or one of the tasks fail.

    """
    original_src_path = src_path
    if src_path:
        repo = vcs.get_repo(src_path)
        if repo:
            src_path = vcs.clone(repo)
    elif src_path and not Path(src_path).is_absolute():
        original_src_path = None

    conf, flags = make_config(**locals())

    try:
        copy_local(flags=flags, **conf.dict())
    except Exception:
        if cleanup_on_error:
            print("Something went wrong. Removing destination folder.")
            shutil.rmtree(dst_path, ignore_errors=True)
        raise
    finally:
        if repo and src_path:
            shutil.rmtree(src_path)


def copy_local(
    src_path: Path,
    dst_path: Path,
    data: AnyByStrDict,
    extra_paths: PathSeq,
    exclude: StrOrPathSeq,
    include: StrOrPathSeq,
    skip_if_exists: StrOrPathSeq,
    tasks: StrSeq,
    envops: Optional[AnyByStrDict],
    original_src_path: str,
    flags: Flags,
) -> None:

    render = get_jinja_renderer(src_path, data, extra_paths, envops, original_src_path)

    skip_if_exists = [render.string(pattern) for pattern in skip_if_exists]
    must_filter, must_skip = get_name_filters(exclude, include, skip_if_exists)

    if not flags.quiet:
        print("")  # padding space

    folder: StrOrPath
    rel_folder: StrOrPath
    for folder, _, files in os.walk(src_path):
        rel_folder = str(folder).replace(str(src_path), "", 1).lstrip(os.path.sep)
        rel_folder = render.string(rel_folder)
        rel_folder = str(rel_folder).replace("." + os.path.sep, ".", 1)

        if must_filter(rel_folder):
            continue

        folder = Path(folder)
        rel_folder = Path(rel_folder)

        render_folder(dst_path, rel_folder, flags)

        source_paths = get_source_paths(folder, rel_folder, files, render, must_filter)
        for source_path, rel_path in source_paths:
            render_file(dst_path, rel_path, source_path, render, must_skip, flags)

    if not flags.quiet:
        print("")  # padding space

    if tasks:
        run_tasks(dst_path, render, tasks)
        if not flags.quiet:
            print("")  # padding space


def get_source_paths(
    folder: Path,
    rel_folder: Path,
    files: StrSeq,
    render: Renderer,
    must_filter: Callable[[StrOrPath], bool],
) -> List[Tuple[Path, Path]]:
    source_paths = []
    files_set = set(files)
    for src_name in files:
        if f"{src_name}.tmpl" in files_set:
            continue
        dst_name = re.sub(RE_TMPL, "", str(src_name))
        dst_name = render.string(dst_name)
        rel_path = rel_folder / dst_name

        if rel_folder == rel_path or must_filter(rel_path):
            continue
        source_paths.append((folder / src_name, rel_path))
    return source_paths


def render_folder(dst_path: Path, rel_folder: Path, flags: Flags) -> None:
    dst_path = dst_path / rel_folder
    rel_path = f"{rel_folder}{os.path.sep}"

    if rel_folder == Path("."):
        if not flags.pretend:
            make_folder(dst_path)
        return

    if dst_path.exists():
        printf("identical", rel_path, style=Style.IGNORE, quiet=flags.quiet)
        return

    if not flags.pretend:
        make_folder(dst_path)

    printf("create", rel_path, style=Style.OK, quiet=flags.quiet)


def render_file(
    dst_path: Path,
    rel_path: Path,
    src_path: Path,
    render: Renderer,
    must_skip: CheckPathFunc,
    flags: Flags,
) -> None:
    """Process or copy a file of the skeleton.
    """
    content: Optional[str] = None
    if src_path.suffix == ".tmpl":
        content = render(src_path)

    dst_path = dst_path / rel_path

    if not dst_path.exists():
        printf("create", rel_path, style=Style.OK, quiet=flags.quiet)
    elif files_are_identical(src_path, dst_path, content):
        printf("identical", rel_path, style=Style.IGNORE, quiet=flags.quiet)
        return
    elif must_skip(rel_path) or not overwrite_file(dst_path, rel_path, flags):
        printf("skip", rel_path, style=Style.WARNING, quiet=flags.quiet)
        return
    else:
        printf("force", rel_path, style=Style.WARNING, quiet=flags.quiet)

    if flags.pretend:
        pass
    elif content is None:
        copy_file(src_path, dst_path)
    else:
        dst_path.write_text(content)


def files_are_identical(src_path: Path, dst_path: Path, content: Optional[str]) -> bool:
    if content is None:
        return filecmp.cmp(str(src_path), str(dst_path), shallow=False)
    return dst_path.read_text() == content


def overwrite_file(dst_path: Path, rel_path: Path, flags: Flags) -> OptBool:
    printf("conflict", rel_path, style=Style.DANGER, quiet=flags.quiet)
    if flags.force:
        return True
    if flags.skip:
        return False
    return prompt_bool(f" Overwrite {dst_path}?", default=True)  # pragma: no cover


def run_tasks(dst_path: StrOrPath, render: Renderer, tasks: StrSeq) -> None:
    for i, task in enumerate(tasks):
        task = render.string(task)
        # TODO: should we respect the `quiet` flag here as well?
        printf(f" > Running task {i + 1} of {len(tasks)}", task, style=Style.OK)
        subprocess.run(task, shell=True, check=True, cwd=dst_path)
