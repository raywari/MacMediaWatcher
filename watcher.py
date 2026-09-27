import os
import time
import json
import errno
import zipfile
import shutil
import logging
import signal
import subprocess
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import yaml
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.yaml"
CONFIG_EXAMPLE = BASE_DIR / "config.example.yaml"

DEFAULTS = {
    "watch_dirs": [str(Path.home() / "Downloads")],
    "ignored_dirs": [],
    "target_folder": "",
    "target_extensions": [".zip", ".avif", ".tif", ".tiff", ".svg"],
    "extract_extensions": [".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm", ".prores"],
    "svg_max_resolution": 2000,
    "max_workers": 4,
    "main_loop_interval": 2,
    "stability_check_interval": 3,
    "stable_checks_required": 2,
    "sips_timeout": 300,
    "max_attempts": 3,
    "retry_delays": [30, 90],
    "min_free_space_mb": 500,
}


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        if CONFIG_EXAMPLE.exists():
            shutil.copy(CONFIG_EXAMPLE, CONFIG_FILE)
            print("Created config.yaml from config.example.yaml — edit it and restart.")
        else:
            print("No config.yaml found. Using defaults.")
            return dict(DEFAULTS)

    with open(CONFIG_FILE, "r") as f:
        user_cfg = yaml.safe_load(f) or {}

    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in user_cfg.items() if v is not None})
    return cfg


cfg = load_config()

WATCH_DIRS = [Path(p).expanduser() for p in cfg["watch_dirs"]]
IGNORED_DIRS = set(cfg["ignored_dirs"])
TARGET_FOLDER_NAME = cfg["target_folder"]

TARGET_EXTENSIONS = set(cfg["target_extensions"])
EXTRACT_EXTENSIONS = set(cfg["extract_extensions"])

MAX_WORKERS = cfg["max_workers"]
MAIN_LOOP_INTERVAL = cfg["main_loop_interval"]
STABILITY_CHECK_INTERVAL = cfg["stability_check_interval"]
STABLE_CHECKS_REQUIRED = cfg["stable_checks_required"]
SIPS_TIMEOUT = cfg["sips_timeout"]
SVG_MAX_RESOLUTION = str(cfg["svg_max_resolution"])

MAX_ATTEMPTS = cfg["max_attempts"]
RETRY_DELAYS = cfg["retry_delays"]
MIN_FREE_SPACE = cfg["min_free_space_mb"] * 1024 * 1024

STATE_FILE = BASE_DIR / "watcher_state.json"
LOG_FILE = BASE_DIR / "watcher.log"

shutdown_event = threading.Event()


class MediaWorkerProcess:
    def __init__(self):
        if len(RETRY_DELAYS) < MAX_ATTEMPTS - 1:
            raise ValueError("retry_delays does not cover max_attempts")

        self.lock = threading.Lock()
        self.output_lock = threading.Lock()
        self.processing_files = set()
        self.failed_files = self._load_failed_state()

        self.executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

        self.logger = logging.getLogger("MediaWorker")
        self.logger.setLevel(logging.INFO)
        if not self.logger.handlers:
            formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
            fh = logging.FileHandler(LOG_FILE)
            ch = logging.StreamHandler()
            fh.setFormatter(formatter)
            ch.setFormatter(formatter)
            self.logger.addHandler(fh)
            self.logger.addHandler(ch)

    def _check_disk_space(self, path: Path, required: int = 0):
        free = shutil.disk_usage(path).free
        needed = max(MIN_FREE_SPACE, required)
        if free < needed:
            raise OSError(
                errno.ENOSPC,
                f"Not enough disk space (free: {free // 1024 // 1024}MB, "
                f"required: {needed // 1024 // 1024}MB)"
            )

    def _load_failed_state(self) -> dict:
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE, "r") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_failed_state(self):
        tmp_file = STATE_FILE.with_suffix('.tmp')
        try:
            with open(tmp_file, "w") as f:
                json.dump(self.failed_files, f, indent=2)
            os.replace(tmp_file, STATE_FILE)
        except Exception as e:
            self.logger.error(f"Failed to save state: {e}")

    def add_failed(self, filepath_str: str, attempt: int):
        with self.lock:
            try:
                current_mtime = os.path.getmtime(filepath_str)
            except OSError:
                current_mtime = 0

            if attempt < MAX_ATTEMPTS:
                delay = RETRY_DELAYS[attempt - 1]
                next_retry = time.time() + delay
                self.failed_files[filepath_str] = {
                    "attempts": attempt,
                    "next_retry": next_retry,
                    "mtime": current_mtime
                }
                self.logger.warning(f"Retry {filepath_str} in {delay}s (attempt {attempt}/{MAX_ATTEMPTS})")
            else:
                self.failed_files[filepath_str] = {
                    "attempts": attempt,
                    "next_retry": None,
                    "mtime": current_mtime
                }
                self.logger.error(f"File {filepath_str} permanently marked as failed.")
            self._save_failed_state()

    def _mark_success(self, filepath_str: str):
        with self.lock:
            if filepath_str in self.failed_files:
                del self.failed_files[filepath_str]
                self._save_failed_state()

    def submit_task(self, filepath: Path, attempt: int = 1):
        if not filepath.exists():
            return

        filepath_str = str(filepath)

        with self.lock:
            if filepath_str in self.processing_files:
                return

            failed_data = self.failed_files.get(filepath_str)
            if failed_data:
                next_retry = failed_data.get("next_retry")

                if next_retry is None:
                    try:
                        current_mtime = os.path.getmtime(filepath_str)
                    except OSError:
                        return

                    if current_mtime > failed_data.get("mtime", 0):
                        self.logger.info(f"Change detected in {filepath.name}, resetting lock.")
                        del self.failed_files[filepath_str]
                        self._save_failed_state()
                    else:
                        return

                elif time.time() < next_retry:
                    return

            self.processing_files.add(filepath_str)

        self.logger.info(f"Queued: {filepath.name}")
        self.executor.submit(self._worker_task, filepath, attempt)

    def check_retries(self):
        current_time = time.time()
        to_retry = []

        with self.lock:
            for filepath_str, data in list(self.failed_files.items()):
                next_retry = data.get("next_retry")
                if next_retry and current_time >= next_retry:
                    to_retry.append((filepath_str, data["attempts"] + 1))

            if to_retry:
                for filepath_str, _ in to_retry:
                    del self.failed_files[filepath_str]
                self._save_failed_state()

        for filepath_str, next_attempt in to_retry:
            self.submit_task(Path(filepath_str), attempt=next_attempt)

    def cleanup_dir_leftovers(self, watch_dir: Path):
        if not watch_dir.is_dir():
            return

        self.logger.info(f"Checking temp files in: {watch_dir.name}...")
        for root, dirs, files in os.walk(watch_dir):
            dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]

            if TARGET_FOLDER_NAME and TARGET_FOLDER_NAME not in Path(root).parts:
                continue

            for d in list(dirs):
                if d.endswith('.__extracting__'):
                    dir_path = Path(root) / d
                    try:
                        shutil.rmtree(dir_path)
                        self.logger.info(f"Cleaned temp dir: {d}")
                        dirs.remove(d)
                    except OSError as e:
                        self.logger.error(f"Failed to remove {d}: {e}")

            for f in files:
                if f.endswith('.__converting__'):
                    file_path = Path(root) / f
                    try:
                        file_path.unlink()
                        self.logger.info(f"Cleaned temp file: {f}")
                    except OSError:
                        pass

    def _wait_for_stability(self, filepath: Path) -> bool:
        last_size = -1
        last_mtime = -1
        stable_count = 0

        while stable_count < STABLE_CHECKS_REQUIRED:
            if not filepath.exists():
                return False

            try:
                stat = filepath.stat()
            except OSError:
                if not filepath.exists():
                    return False
                raise

            current_size = stat.st_size
            current_mtime = stat.st_mtime

            if current_size == last_size and current_mtime == last_mtime and current_size > 0:
                stable_count += 1
            else:
                stable_count = 0

            last_size = current_size
            last_mtime = current_mtime

            if stable_count < STABLE_CHECKS_REQUIRED:
                time.sleep(STABILITY_CHECK_INTERVAL)

        return True

    def _process_zip(self, zip_path: Path):
        parent_dir = zip_path.parent
        temp_dir = parent_dir / (zip_path.name + ".__extracting__")
        extracted_paths = []

        try:
            with zipfile.ZipFile(zip_path, 'r') as z:
                total_size = sum(
                    info.file_size for info in z.infolist()
                    if info.filename and not info.is_dir()
                    and '__MACOSX' not in info.filename
                    and not info.filename.endswith('.DS_Store')
                    and Path(info.filename).suffix.lower() in EXTRACT_EXTENSIONS
                )
                self._check_disk_space(parent_dir, total_size)

                temp_dir.mkdir(exist_ok=True)

                for item in z.namelist():
                    if '__MACOSX' in item or item.endswith('.DS_Store'):
                        continue

                    path_obj = Path(item)
                    if path_obj.suffix.lower() in EXTRACT_EXTENSIONS:
                        target_temp = temp_dir / path_obj.name

                        counter = 1
                        while target_temp.exists():
                            target_temp = temp_dir / f"{path_obj.stem}_{counter}{path_obj.suffix}"
                            counter += 1

                        with z.open(item) as source, open(target_temp, "wb") as target:
                            shutil.copyfileobj(source, target)
                        extracted_paths.append(target_temp)

            with self.output_lock:
                for temp_file in extracted_paths:
                    final_target = parent_dir / temp_file.name
                    counter = 1
                    while final_target.exists():
                        final_target = parent_dir / f"{temp_file.stem}_{counter}{temp_file.suffix}"
                        counter += 1
                    shutil.move(str(temp_file), str(final_target))
                    self.logger.info(f"Extracted: {final_target.name}")

            if temp_dir.exists():
                shutil.rmtree(temp_dir)

            zip_path.unlink()
            self._mark_success(str(zip_path))
            self.logger.info(f"ZIP {zip_path.name} processed and deleted.")

        except Exception:
            if temp_dir.exists():
                shutil.rmtree(temp_dir)
            raise

    def _convert_with_sips(self, src_path: Path, output_format: str, output_suffix: str, extra_args=None):
        temp_file = None
        try:
            self._check_disk_space(src_path.parent)

            with self.output_lock:
                out_path = src_path.with_suffix(output_suffix)
                counter = 1
                while out_path.exists() or (out_path.parent / (out_path.name + ".__converting__")).exists():
                    out_path = src_path.parent / f"{src_path.stem}_{counter}{output_suffix}"
                    counter += 1

                temp_file = out_path.parent / (out_path.name + ".__converting__")
                temp_file.touch()

            cmd = ['sips', '-s', 'format', output_format]
            if extra_args:
                cmd.extend(extra_args)
            cmd.extend([str(src_path), '--out', str(temp_file)])

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=SIPS_TIMEOUT)

            if result.returncode == 0:
                temp_file.replace(out_path)
                src_path.unlink()
                self._mark_success(str(src_path))
                self.logger.info(f"{src_path.suffix.upper()} converted to {out_path.name}")
            else:
                raise RuntimeError(f"sips error: {result.stderr.strip()}")
        finally:
            if temp_file and temp_file.exists():
                temp_file.unlink()

    def _worker_task(self, filepath: Path, attempt: int):
        try:
            if not filepath.exists():
                return

            if self._wait_for_stability(filepath):
                self.logger.info(f"Processing: {filepath.name}")
                suffix = filepath.suffix.lower()

                if suffix == '.zip':
                    self._process_zip(filepath)
                elif suffix in {'.avif', '.tif', '.tiff'}:
                    self._convert_with_sips(filepath, 'jpeg', '.jpg')
                elif suffix == '.svg':
                    self._convert_with_sips(filepath, 'png', '.png', ['-Z', SVG_MAX_RESOLUTION])
            else:
                self.logger.warning(f"File {filepath.name} was removed while waiting.")
        except OSError as e:
            if e.errno == errno.ENOSPC:
                self.logger.error(f"Not enough disk space to process {filepath.name}")
            else:
                self.logger.error(f"Failed {filepath.name}: {e}")
            self.add_failed(str(filepath), attempt)
        except subprocess.TimeoutExpired:
            self.logger.error(f"sips timeout for {filepath.name}")
            self.add_failed(str(filepath), attempt)
        except Exception as e:
            self.logger.error(f"Failed {filepath.name}: {e}")
            self.add_failed(str(filepath), attempt)
        finally:
            with self.lock:
                self.processing_files.discard(str(filepath))

    def shutdown(self):
        self.logger.info("Waiting for threads to finish...")
        self.executor.shutdown(wait=True)


class FolderWatcher(FileSystemEventHandler):
    def __init__(self, worker: MediaWorkerProcess):
        self.worker = worker

    def _handle_event(self, event):
        if event.is_directory:
            return

        filepath = Path(event.src_path)
        if filepath.name.startswith('.'):
            return

        if not set(filepath.parts).isdisjoint(IGNORED_DIRS):
            return

        if TARGET_FOLDER_NAME and TARGET_FOLDER_NAME not in filepath.parts:
            return

        if filepath.suffix.lower() in TARGET_EXTENSIONS:
            self.worker.submit_task(filepath)

    def on_created(self, event): self._handle_event(event)
    def on_modified(self, event): self._handle_event(event)


def scan_directory(worker: MediaWorkerProcess, watch_dir: Path):
    worker.cleanup_dir_leftovers(watch_dir)
    worker.logger.info(f"Initial scan: {watch_dir}...")

    for root, dirs, files in os.walk(watch_dir):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]

        if TARGET_FOLDER_NAME and TARGET_FOLDER_NAME not in Path(root).parts:
            continue

        for file in files:
            if file.startswith('.'):
                continue

            path = Path(root) / file
            if path.suffix.lower() in TARGET_EXTENSIONS:
                worker.submit_task(path)


active_watches = {}


def sync_watches(observer: Observer, worker: MediaWorkerProcess, event_handler: FolderWatcher):
    for d in WATCH_DIRS:
        if d.is_dir() and d not in active_watches:
            try:
                watch = observer.schedule(event_handler, str(d), recursive=True)
                active_watches[d] = watch
                worker.logger.info(f"Watching: {d}")
                scan_directory(worker, d)
            except Exception as e:
                worker.logger.error(f"Failed to watch {d}: {e}")

        elif not d.is_dir() and d in active_watches:
            worker.logger.warning(f"Directory unavailable: {d}")
            try:
                observer.unschedule(active_watches[d])
            except Exception:
                pass
            del active_watches[d]


def main():
    worker = MediaWorkerProcess()
    event_handler = FolderWatcher(worker)
    observer = Observer()

    def handle_signal(signum, _frame):
        worker.logger.info(f"Received signal {signum}, shutting down...")
        shutdown_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    sync_watches(observer, worker, event_handler)

    if not active_watches:
        worker.logger.warning("No target directories available. Waiting for connection...")

    try:
        observer.start()
    except Exception as e:
        worker.logger.critical(f"Observer failed to start: {e}")
        return

    while not shutdown_event.is_set():
        sync_watches(observer, worker, event_handler)
        worker.check_retries()
        shutdown_event.wait(MAIN_LOOP_INTERVAL)

    worker.logger.info("Shutting down...")
    observer.stop()
    worker.shutdown()
    worker.logger.info("Stopped.")
    observer.join()


if __name__ == "__main__":
    main()