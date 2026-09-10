import os
import re
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from kelso.lib.apps import AppID
from kelso.lib.stack import AppStack

# The bundle flavors kelso knows, by filename suffix. These are the one
# source of truth; everything that names a catalog entry derives from them.
KLSO_SUFFIX = ".klso"
KLSO_MD_SUFFIX = ".klso.md"
KLSO_TAR_SUFFIX = ".klso.tar.gz"

# Markdown bundles are meant to be readable in one sitting; bigger bundles use the
# folder format.
KLSO_MD_CUTOFF_KB = 128

# Group1: lang, group2: path, group3: optional ":+x"
KLSO_MD_FILE_PATTERN = re.compile(r'^```(\w*)\s+klso_path="([^"]+?)(:\+x)?"\s*$')


class KelsoApp:
  """A kelso application bundle on the filesystem"""

  SUFFIX = ""

  path: Path
  app_id: AppID

  def files(self) -> Iterator[Path]: ...

  def app_stack(self) -> AppStack: ...

  def extract_to(self, target: Path): ...


class BundleFolder(KelsoApp):
  SUFFIX = KLSO_SUFFIX

  def __init__(self, path: Path, app_id: AppID):
    self.path = path
    self.app_id = app_id

  def files(self) -> Iterator[Path]:
    return self.path.rglob("*")

  def app_stack(self) -> AppStack:
    return AppStack.from_file(self.path / "manifest.toml", self.app_id)

  def extract_to(self, target: Path):
    target.mkdir(parents=True, exist_ok=True)
    shutil.copytree(self.path, target, dirs_exist_ok=True)


@dataclass(frozen=True)
class MdFile:
  path: str
  executable: bool
  content: str


@dataclass(frozen=True)
class MdFileList:
  """What `extract_md_files` saw -- the files plus how the scan ended."""

  files: list[MdFile]
  unclosed_block: bool


class BundleMdFile(KelsoApp):
  SUFFIX = KLSO_MD_SUFFIX

  def __init__(self, path: Path, app_id: AppID, files: list[MdFile]):
    self.path = path
    self.app_id = app_id
    self._files = files

  def files(self) -> Iterator[Path]:
    return (Path(file.path) for file in self._files)

  def app_stack(self) -> AppStack:
    for md_file in self._files:
      if md_file.path == "manifest.toml":
        return AppStack.from_bytes(md_file.content.encode(), self.app_id, self.path)
    raise ValueError(f"{self.path.name} is missing a manifest.toml file")

  def extract_to(self, target: Path):
    target.mkdir(parents=True, exist_ok=True)
    for md_file in self._files:
      dest = target / md_file.path
      dest.parent.mkdir(parents=True, exist_ok=True)
      dest.write_text(md_file.content + "\n")
      if md_file.executable:
        dest.chmod(dest.stat().st_mode | 0o111)


class BundleTarFile(KelsoApp):
  SUFFIX = KLSO_TAR_SUFFIX

  ## TDOO: Support tar.gz kelso apps.
  def __init__(self, path: Path, app_id: AppID):
    self.path = path
    self.app_id = app_id

  def files(self) -> Iterator[Path]:
    raise NotImplementedError("tar.gz kelso apps are not supported yet")

  def app_stack(self) -> AppStack:
    raise NotImplementedError("tar.gz kelso apps are not supported yet")

  def extract_to(self, target: Path):
    raise NotImplementedError("tar.gz kelso apps are not supported yet")


def app_id_from_path(path: Path) -> AppID:
  """The app id a bundle path carries: `<id>.klso` dir or `<id>.klso.md` file."""
  if path.name.endswith(KLSO_MD_SUFFIX):
    if not path.is_file():
      raise ValueError(f"{path} is not a file")
    return AppID(path.name.removesuffix(KLSO_MD_SUFFIX))
  if not path.is_dir():
    raise ValueError(f"{path} is not a directory")
  if path.suffix != KLSO_SUFFIX:
    raise ValueError(f"{path} is not a bundle: directory name must end in .klso")
  if not (path / "manifest.toml").is_file():
    raise ValueError(f"{path} is not a bundle: missing manifest.toml")
  return AppID(path.stem)


def is_pathlike(raw: str) -> bool:
  """Determine if an argument looks like a filesystem path of a kelso app."""
  return (
    os.sep in raw
    or raw.startswith(("~", "."))
    or raw.endswith((KLSO_SUFFIX, KLSO_MD_SUFFIX))
  )


def could_be_bundle(path: Path) -> bool:
  if path.is_dir():
    return (
      path.name.endswith(BundleFolder.SUFFIX) and (path / "manifest.toml").is_file()
    )
  if path.is_file():
    return path.name.endswith(BundleMdFile.SUFFIX) or path.name.endswith(
      BundleTarFile.SUFFIX
    )
  return False


def scan_bundles(path: Path) -> Iterator[tuple[str, Path]]:
  """Every bundle directly under `path`: (app id, path relative to `path`)."""
  if not path.is_dir():
    return
  for entry in sorted(path.iterdir()):
    if not could_be_bundle(entry):
      continue
    name = entry.name
    # Longest suffix first
    for suffix in (KLSO_TAR_SUFFIX, KLSO_MD_SUFFIX, KLSO_SUFFIX):
      if name.endswith(suffix):
        yield name.removesuffix(suffix), entry.relative_to(path)
        break


def manifest_text(path: Path) -> str:
  """A bundle's `manifest.toml` as raw text, parseable or not."""
  if path.is_dir():
    try:
      return (path / "manifest.toml").read_text()
    except OSError:
      return ""
  if path.name.endswith(KLSO_MD_SUFFIX):
    try:
      files = extract_md_files(path.read_text())
    except OSError:
      return ""
    for md_file in files.files:
      if md_file.path == "manifest.toml":
        return md_file.content
  return ""


def load_bundle(path: Path) -> KelsoApp:
  if not could_be_bundle(path):
    raise ValueError(f"{path.name} does not seem to be a valid kelso app.")

  name = path.name

  if name.endswith(BundleFolder.SUFFIX):
    # Known: If name ends with .klso after could_be_bundle, it has a toml file.
    return load_bundle_folder(path, AppID(name.removesuffix(BundleFolder.SUFFIX)))

  if name.endswith(BundleMdFile.SUFFIX):
    return load_bundle_md(path, AppID(name.removesuffix(BundleMdFile.SUFFIX)))

  if name.endswith(BundleTarFile.SUFFIX):
    return load_bundle_tar_gz(path, AppID(name.removesuffix(BundleTarFile.SUFFIX)))

  raise ValueError(f"{path.name} is not a valid kelso app.")


def load_bundle_folder(path: Path, app_id: AppID) -> BundleFolder:
  if not path.is_dir():
    raise ValueError(f"{path.name} is not a directory")
  if not (path / "manifest.toml").is_file():
    raise ValueError(f"{path.name} is not a valid kelso app: missing manifest.toml")

  return BundleFolder(path, app_id)


def extract_md_files(content: str) -> MdFileList:
  """Gather the contents of markdown code blocks that carry a klso_path attribute."""
  files = []

  current_path = None
  current_content = []
  ex = False

  for line in content.splitlines():
    if current_path is None:
      match = KLSO_MD_FILE_PATTERN.match(line)
      if match:
        current_path = match.group(2)
        ex = bool(match.group(3))
        current_content = []
    else:
      if line.strip() == "```":
        # End of file
        files.append(
          MdFile(path=current_path, executable=ex, content="\n".join(current_content))
        )
        current_path = None
        current_content = []
        ex = False
      else:
        current_content.append(line)

  if current_path is not None:
    files.append(
      MdFile(path=current_path, executable=ex, content="\n".join(current_content))
    )
    unclosed_block = True
  else:
    unclosed_block = False

  return MdFileList(files=files, unclosed_block=unclosed_block)


def load_bundle_md(path: Path, app_id: AppID) -> BundleMdFile:
  st_size_kb = path.stat().st_size / 1024
  if st_size_kb > KLSO_MD_CUTOFF_KB:
    raise ValueError(
      f"{path.name} is too large to load as a .klso.md file ({st_size_kb} > {KLSO_MD_CUTOFF_KB})kb"
    )

  with open(path) as f:
    content = f.read()

  files = extract_md_files(content)

  problems = []
  if files.unclosed_block:
    problems.append(f"{path.name} has an unclosed file block {files.files[-1].path}")
  if not files.files:
    problems.append(f"{path.name} does not contain any files")
  if not any(f.path == "manifest.toml" for f in files.files):
    problems.append(f"{path.name} is missing a manifest.toml file")

  for md_file in files.files:
    p = Path(md_file.path)
    if p.is_absolute():
      problems.append(f"{path.name} has absolute file paths ({p})")
    if ".." in p.parts:
      problems.append(f"{path.name} has files paths that traverse up ({p})")
    if len(md_file.content) == 0:
      problems.append(f"{path.name} has empty file ({p})")

  if problems:
    raise ValueError(f"{path.name} invalid .klso.md file: {', '.join(problems)}")

  return BundleMdFile(path, app_id, files.files)


def load_bundle_tar_gz(path: Path, app_id: AppID) -> BundleTarFile:
  raise ValueError("tar.gz kelso apps are not supported yet")
