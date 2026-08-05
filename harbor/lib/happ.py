import os
import re
import shutil
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from harbor.lib.apps import AppID
from harbor.lib.stack import AppStack, app_stack

# The bundle flavors harbor knows, by filename suffix. These are the one
# source of truth; everything that names a catalog entry derives from them.
HAPP_SUFFIX = ".happ"
HAPP_MD_SUFFIX = ".happ.md"
HAPP_TAR_SUFFIX = ".happ.tar.gz"

# The point of markdown files is to be frictonless to audit and understand
# Bigger happs are absolutely supported, but larger than this is an anti-pattern.
# This was chosen to be about 10x as long as what I considered "reasonable"
HAPP_MD_CUTOFF_KB = 128

# Group1: lang, group2: path, group3: optional ":+x"
HAPP_MD_FILE_PATTERN = re.compile(r'^```(\w*)\s+happ_path="([^"]+?)(:\+x)?"\s*$')


class HarborApp:
  """A harbor application bundle on the filesystem"""

  SUFFIX = ""

  path: Path
  app_id: AppID

  def files(self) -> Iterator[Path]: ...

  def app_stack(self) -> AppStack: ...

  def extract_to(self, target: Path): ...


class HappFolder(HarborApp):
  SUFFIX = HAPP_SUFFIX

  def __init__(self, path: Path, app_id: AppID):
    self.path = path
    self.app_id = app_id

  def files(self) -> Iterator[Path]:
    return self.path.rglob("*")

  def app_stack(self) -> AppStack:
    return app_stack(self.path, self.app_id)

  def extract_to(self, target: Path):
    target.mkdir(parents=True, exist_ok=True)
    shutil.copytree(self.path, target, dirs_exist_ok=True)


@dataclass(frozen=True)
class MdFile:
  executable: bool
  content: str


class HappMdFile(HarborApp):
  SUFFIX = HAPP_MD_SUFFIX

  def __init__(self, path: Path, app_id: AppID, files: dict[str, MdFile]):
    self.path = path
    self.app_id = app_id
    self._files = files

  def files(self) -> Iterator[Path]:
    return (Path(path) for path in self._files.keys())

  def app_stack(self) -> AppStack:
    # The manifest parser reads from disk, so materialize the bundle in a
    # scratch dir; a .happ.md is small by construction (HAPP_MD_CUTOFF_KB).
    with tempfile.TemporaryDirectory() as tmp:
      extracted = Path(tmp) / "happ"
      self.extract_to(extracted)
      return app_stack(extracted, self.app_id)

  def extract_to(self, target: Path):
    target.mkdir(parents=True, exist_ok=True)
    for rel_path, md_file in self._files.items():
      dest = target / rel_path
      dest.parent.mkdir(parents=True, exist_ok=True)
      dest.write_text(md_file.content + "\n")
      if md_file.executable:
        dest.chmod(dest.stat().st_mode | 0o111)


class HappTarFile(HarborApp):
  SUFFIX = HAPP_TAR_SUFFIX

  ## TDOO: Support tar.gz harbor apps.
  def __init__(self, path: Path, app_id: AppID):
    self.path = path
    self.app_id = app_id

  def files(self) -> Iterator[Path]:
    raise NotImplementedError("tar.gz harbor apps are not supported yet")

  def app_stack(self) -> AppStack:
    raise NotImplementedError("tar.gz harbor apps are not supported yet")

  def extract_to(self, target: Path):
    raise NotImplementedError("tar.gz harbor apps are not supported yet")


def app_id_from_path(path: Path) -> AppID:
  """The app id a bundle path carries: `<id>.happ` dir or `<id>.happ.md` file."""
  if path.name.endswith(HAPP_MD_SUFFIX):
    if not path.is_file():
      raise ValueError(f"{path} is not a file")
    return AppID(path.name.removesuffix(HAPP_MD_SUFFIX))
  if not path.is_dir():
    raise ValueError(f"{path} is not a directory")
  if path.suffix != HAPP_SUFFIX:
    raise ValueError(f"{path} is not a happ bundle: directory name must end in .happ")
  if not (path / "manifest.toml").is_file():
    raise ValueError(f"{path} is not a happ bundle: missing manifest.toml")
  return AppID(path.stem)


def is_pathlike(raw: str) -> bool:
  """Decide whether an APP argument names a filesystem path vs an app id.

  A valid happ path ends in a flavor suffix, so a bare name without one can
  never be a path.
  """
  return (
    os.sep in raw
    or raw.startswith(("~", "."))
    or raw.endswith((HAPP_SUFFIX, HAPP_MD_SUFFIX))
  )


def could_be_happ(path: Path) -> bool:
  if path.is_dir():
    return path.name.endswith(HappFolder.SUFFIX) and (path / "manifest.toml").is_file()
  if path.is_file():
    return path.name.endswith(HappMdFile.SUFFIX) or path.name.endswith(
      HappTarFile.SUFFIX
    )
  return False


def scan_happs(path: Path) -> Iterator[tuple[str, Path]]:
  """Every bundle directly under `path`: (app id, path relative to `path`).

  Only entries that pass `could_be_happ` are yielded, in name order -- so a
  `<id>.happ` folder sorts before a `<id>.happ.md` file, and a caller keeping
  the first entry per id prefers the folder flavor.
  """
  if not path.is_dir():
    return
  for entry in sorted(path.iterdir()):
    if not could_be_happ(entry):
      continue
    name = entry.name
    # Longest suffix first: ".happ" would never match the others anyway, but
    # the order makes that not worth thinking about.
    for suffix in (HAPP_TAR_SUFFIX, HAPP_MD_SUFFIX, HAPP_SUFFIX):
      if name.endswith(suffix):
        yield name.removesuffix(suffix), entry.relative_to(path)
        break


def load_happ(path: Path) -> HarborApp:
  if not could_be_happ(path):
    raise ValueError(f"{path.name} does not seem to be a valid harbor app.")

  name = path.name

  if name.endswith(HappFolder.SUFFIX):
    # Known: If name ends with .happ after could_be_happ, it has a toml file.
    return load_happ_folder(path, AppID(name.removesuffix(HappFolder.SUFFIX)))

  if name.endswith(HappMdFile.SUFFIX):
    return load_happ_md(path, AppID(name.removesuffix(HappMdFile.SUFFIX)))

  if name.endswith(HappTarFile.SUFFIX):
    return load_happ_tar_gz(path, AppID(name.removesuffix(HappTarFile.SUFFIX)))

  raise ValueError(f"{path.name} is not a valid harbor app.")


def load_happ_folder(path: Path, app_id: AppID) -> HappFolder:
  if not path.is_dir():
    raise ValueError(f"{path.name} is not a directory")
  if not (path / "manifest.toml").is_file():
    raise ValueError(f"{path.name} is not a valid harbor app: missing manifest.toml")

  return HappFolder(path, app_id)


def load_happ_md(path: Path, app_id: AppID) -> HappMdFile:
  st_size_kb = path.stat().st_size / 1024
  if st_size_kb > HAPP_MD_CUTOFF_KB:
    raise ValueError(
      f"{path.name} is too large to load as a happ.md file ({st_size_kb} > {HAPP_MD_CUTOFF_KB})kb"
    )

  with open(path) as f:
    content = f.read()

  files = {}

  current_path = None
  current_content = []
  ex = False

  for line in content.splitlines():
    if current_path is None:
      match = HAPP_MD_FILE_PATTERN.match(line)
      if match:
        current_path = match.group(2)
        ex = bool(match.group(3))
        current_content = []
    else:
      if line.strip() == "```":
        # End of file
        files[current_path] = MdFile(executable=ex, content="\n".join(current_content))
        current_path = None
        current_content = []
        ex = False
      else:
        current_content.append(line)

  # Validate
  problems = []
  if current_path is not None:
    problems.append(f"{path.name} has an unclosed file block ({current_path})")
  if not files:
    problems.append(f"{path.name} does not contain any files")
  if "manifest.toml" not in files:
    problems.append(f"{path.name} is missing a manifest.toml file")

  for fpath, md_file in files.items():
    p = Path(fpath)
    if p.is_absolute():
      problems.append(f"{path.name} has absolute file paths ({fpath})")
    if ".." in p.parts:
      problems.append(f"{path.name} has files paths that traverse up ({fpath})")
    if len(md_file.content) == 0:
      problems.append(f"{path.name} has empty files ({fpath})")

  if problems:
    raise ValueError(f"{path.name} invalid happ.md file: {', '.join(problems)}")

  return HappMdFile(path, app_id, files)


def load_happ_tar_gz(path: Path, app_id: AppID) -> HappTarFile:
  raise ValueError("tar.gz harbor apps are not supported yet")
