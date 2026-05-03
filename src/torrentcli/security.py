"""Security: ClamAV scanning, file type enforcement, and quarantine."""

import asyncio
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.console import Console

console = Console()


@dataclass
class ScanResult:
    """Result of a security scan on a file or directory."""
    path: str = ""
    clean: bool = True
    threats: list[str] = field(default_factory=list)
    blocked_files: list[str] = field(default_factory=list)
    scanned_files: int = 0
    scan_time_seconds: float = 0.0
    error: str = ""

    @property
    def summary(self) -> str:
        if self.clean:
            if self.error:
                return f"Clean (warning: {self.error})"
            return f"Clean — {self.scanned_files} files scanned in {self.scan_time_seconds:.1f}s"
        issues = []
        if self.threats:
            issues.append(f"{len(self.threats)} threat(s)")
        if self.blocked_files:
            issues.append(f"{len(self.blocked_files)} blocked file type(s)")
        if self.error and not issues:
            return f"Scan error: {self.error}"
        suffix = f" (note: {self.error})" if self.error else ""
        return f"FLAGGED — {', '.join(issues)}{suffix}"


# Default blocked extensions — executables and scripts that have no business
# being in a media download
DEFAULT_BLOCKED_EXTENSIONS = {
    ".exe", ".bat", ".cmd", ".ps1", ".scr", ".vbs", ".vbe",
    ".msi", ".msp", ".dll", ".com", ".pif", ".wsf", ".wsh",
    ".js", ".jse", ".hta", ".cpl", ".inf", ".reg", ".rgs",
    ".lnk", ".app", ".action", ".command",  # macOS executables
}


def is_audiobook_dir(path: str | Path) -> bool:
    """True if path is an .m4b file or contains any .m4b file (recursive).

    M4B presence is the canonical signal for an audiobook download — it's
    the format Apple/Audible use, and bare .mp3 audiobooks are too
    ambiguous (could be music) to re-route automatically.
    """
    path = Path(path)
    if not path.exists():
        return False
    if path.is_file():
        return path.suffix.lower() == ".m4b"
    for entry in path.rglob("*"):
        if entry.is_file() and entry.suffix.lower() == ".m4b":
            return True
    return False


class SecurityScanner:
    """File security scanning and quarantine management."""

    def __init__(self, config: dict):
        self.enabled = config.get("clamav_enabled", True)
        self.clamav_socket = config.get("clamav_socket", "/tmp/clamd.socket")
        self.quarantine_dir = Path(
            config.get("quarantine_dir", "~/Downloads/quarantine")
        ).expanduser()
        self.scan_on_complete = config.get("scan_on_complete", True)
        self.block_password_archives = config.get(
            "block_password_protected_archives", True
        )

        # Build blocked extensions set
        blocked_raw = config.get("blocked_extensions", [])
        if blocked_raw:
            self.blocked_extensions = {
                ext if ext.startswith(".") else f".{ext}"
                for ext in blocked_raw
            }
        else:
            self.blocked_extensions = DEFAULT_BLOCKED_EXTENSIONS.copy()

        # Ensure quarantine directory exists
        self.quarantine_dir.mkdir(parents=True, exist_ok=True)
        self._log_path = self.quarantine_dir / "scan_log.txt"

    # ── ClamAV scanning ──────────────────────────────────────────────────

    async def scan_clamav(self, path: str | Path) -> ScanResult:
        """Scan a file or directory with ClamAV daemon (clamdscan).

        Falls back to clamscan if clamd is not running.
        """
        path = Path(path)
        result = ScanResult(path=str(path))

        if not self.enabled:
            result.clean = True
            return result

        if not path.exists():
            result.error = f"Path does not exist: {path}"
            result.clean = False
            return result

        start = asyncio.get_event_loop().time()

        try:
            # Try clamdscan first (faster, uses daemon)
            proc = await asyncio.create_subprocess_exec(
                "clamdscan",
                "--no-summary",
                "--infected",
                "--recursive",
                str(path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            scanner_used = "clamdscan"

            # If clamd not running, fall back to clamscan
            if proc.returncode == 2 and b"clamd" in stderr.lower():
                proc = await asyncio.create_subprocess_exec(
                    "clamscan",
                    "--no-summary",
                    "--infected",
                    "--recursive",
                    str(path),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
                scanner_used = "clamscan"

        except FileNotFoundError:
            result.error = "ClamAV not installed (install with: brew install clamav)"
            result.clean = False
            return result
        except Exception as e:
            result.error = f"Scan failed: {e}"
            result.clean = False
            return result

        elapsed = asyncio.get_event_loop().time() - start
        result.scan_time_seconds = round(elapsed, 2)

        # Parse output — infected files show as: /path/to/file: ThreatName FOUND
        output = stdout.decode("utf-8", errors="replace")
        for line in output.strip().splitlines():
            line = line.strip()
            if line.endswith("FOUND"):
                result.threats.append(line)
                result.clean = False

        # Count scanned files
        if path.is_file():
            result.scanned_files = 1
        else:
            result.scanned_files = sum(1 for _ in path.rglob("*") if _.is_file())

        # Return code: 0 = clean, 1 = infected found, 2 = error
        if proc.returncode == 2 and not result.threats:
            stderr_text = stderr.decode("utf-8", errors="replace")
            result.error = f"Scanner error: {stderr_text[:200]}"
            result.clean = False

        return result

    # ── File type enforcement ─────────────────────────────────────────────

    def check_file_types(self, path: str | Path) -> list[str]:
        """Find files with blocked extensions in a download directory.

        Returns list of blocked file paths.
        """
        path = Path(path)
        blocked = []

        if path.is_file():
            if path.suffix.lower() in self.blocked_extensions:
                blocked.append(str(path))
        else:
            for f in path.rglob("*"):
                if f.is_file() and f.suffix.lower() in self.blocked_extensions:
                    blocked.append(str(f))

        return blocked

    # ── Size verification ─────────────────────────────────────────────────

    @staticmethod
    def verify_size(path: str | Path, expected_bytes: int, tolerance: float = 0.05) -> bool:
        """Check actual download size against expected.

        Returns True if within tolerance (default 5%).
        """
        path = Path(path)
        if not path.exists():
            return False

        if expected_bytes <= 0:
            return True  # Can't verify without expected size

        if path.is_file():
            actual = path.stat().st_size
        else:
            actual = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())

        if expected_bytes == 0:
            return True

        ratio = actual / expected_bytes
        return (1 - tolerance) <= ratio <= (1 + tolerance)

    # ── Password-protected archive detection ──────────────────────────────

    async def check_password_protected(self, path: str | Path) -> bool:
        """Check if any archives in the download are password-protected.

        Returns True if password-protected archives are found.
        """
        if not self.block_password_archives:
            return False

        path = Path(path)
        archive_extensions = {".zip", ".rar", ".7z"}

        files_to_check = []
        if path.is_file() and path.suffix.lower() in archive_extensions:
            files_to_check.append(path)
        elif path.is_dir():
            for f in path.rglob("*"):
                if f.is_file() and f.suffix.lower() in archive_extensions:
                    files_to_check.append(f)

        for archive in files_to_check:
            try:
                if archive.suffix.lower() == ".zip":
                    proc = await asyncio.create_subprocess_exec(
                        "unzip", "-t", str(archive),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    _, stderr = await proc.communicate()
                    if b"password" in stderr.lower() or b"encrypted" in stderr.lower():
                        return True

                elif archive.suffix.lower() == ".rar":
                    proc = await asyncio.create_subprocess_exec(
                        "unrar", "t", str(archive),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    _, stderr = await proc.communicate()
                    if b"password" in stderr.lower() or b"encrypted" in stderr.lower():
                        return True
            except FileNotFoundError:
                continue  # Tool not installed, skip check

        return False

    # ── Full scan pipeline ────────────────────────────────────────────────

    async def full_scan(
        self,
        path: str | Path,
        expected_bytes: int = 0,
    ) -> ScanResult:
        """Run the complete security pipeline on a download.

        1. File type enforcement (blocked extensions)
        2. Size verification
        3. Password-protected archive detection
        4. ClamAV virus scan

        Returns combined ScanResult.
        """
        path = Path(path)
        result = ScanResult(path=str(path))

        # Step 1: Blocked file types
        blocked = self.check_file_types(path)
        if blocked:
            result.blocked_files = blocked
            result.clean = False
            console.print(
                f"[yellow]⚠ Blocked file types found:[/] {len(blocked)} file(s)"
            )
            for f in blocked[:5]:  # Show first 5
                console.print(f"  [yellow]•[/] {f}")

        # Step 2: Size verification
        if expected_bytes > 0:
            if not self.verify_size(path, expected_bytes):
                result.clean = False
                result.error = "Size mismatch — actual size differs from expected by >5%"
                console.print(f"[yellow]⚠ Size mismatch for {path.name}[/]")

        # Step 3: Password-protected archives
        has_password = await self.check_password_protected(path)
        if has_password:
            result.clean = False
            result.blocked_files.append("PASSWORD_PROTECTED_ARCHIVE")
            console.print(f"[yellow]⚠ Password-protected archive detected[/]")

        # Step 4: ClamAV scan
        if self.enabled:
            clam_result = await self.scan_clamav(path)
            result.scanned_files = clam_result.scanned_files
            result.scan_time_seconds = clam_result.scan_time_seconds

            if clam_result.threats:
                result.threats = clam_result.threats
                result.clean = False
            if clam_result.error:
                # ClamAV errors are informational — don't override clean status
                # unless there were actual threats
                result.error = clam_result.error

        return result

    # ── Quarantine ────────────────────────────────────────────────────────

    def quarantine(self, path: str | Path, scan_result: ScanResult) -> Path:
        """Move a flagged download to quarantine with metadata.

        Returns the quarantine destination path.
        """
        path = Path(path)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest_name = f"{timestamp}_{path.name}"
        dest = self.quarantine_dir / dest_name

        # Move to quarantine
        if path.is_dir():
            shutil.move(str(path), str(dest))
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(dest))

        # Log the quarantine event
        self._log_quarantine(dest, scan_result)

        console.print(f"[red]🔒 Quarantined:[/] {path.name} → {dest}")
        return dest

    def _log_quarantine(self, dest: Path, scan_result: ScanResult):
        """Append quarantine event to log file."""
        timestamp = datetime.now().isoformat()
        with open(self._log_path, "a") as f:
            f.write(f"\n{'='*60}\n")
            f.write(f"Timestamp: {timestamp}\n")
            f.write(f"Path: {scan_result.path}\n")
            f.write(f"Quarantined to: {dest}\n")
            f.write(f"Summary: {scan_result.summary}\n")
            if scan_result.threats:
                f.write("Threats:\n")
                for t in scan_result.threats:
                    f.write(f"  - {t}\n")
            if scan_result.blocked_files:
                f.write("Blocked files:\n")
                for b in scan_result.blocked_files:
                    f.write(f"  - {b}\n")
            if scan_result.error:
                f.write(f"Error: {scan_result.error}\n")

    def release_from_quarantine(self, name: str, dest_dir: str | Path) -> bool:
        """Release a file/folder from quarantine to a destination.

        Use for false positives.
        """
        quarantined = self.quarantine_dir / name
        if not quarantined.exists():
            console.print(f"[red]Not found in quarantine: {name}[/]")
            return False

        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)

        shutil.move(str(quarantined), str(dest / quarantined.name.split("_", 2)[-1]))
        console.print(f"[green]Released from quarantine:[/] {name} → {dest}")

        # Log release
        with open(self._log_path, "a") as f:
            f.write(f"\nRELEASED: {name} → {dest} at {datetime.now().isoformat()}\n")

        return True

    def list_quarantine(self) -> list[dict]:
        """List all quarantined items."""
        items = []
        for entry in sorted(self.quarantine_dir.iterdir()):
            if entry.name == "scan_log.txt":
                continue
            size = (
                entry.stat().st_size
                if entry.is_file()
                else sum(f.stat().st_size for f in entry.rglob("*") if f.is_file())
            )
            items.append({
                "name": entry.name,
                "size": size,
                "is_dir": entry.is_dir(),
                "modified": datetime.fromtimestamp(entry.stat().st_mtime).isoformat(),
            })
        return items

    # ── Connection check ──────────────────────────────────────────────────

    async def check_clamav_available(self) -> dict[str, Any]:
        """Check if ClamAV is installed and daemon is running."""
        status = {
            "installed": False,
            "daemon_running": False,
            "version": "",
            "db_date": "",
        }

        # Check if clamscan exists
        try:
            proc = await asyncio.create_subprocess_exec(
                "clamscan", "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode == 0:
                status["installed"] = True
                status["version"] = stdout.decode().strip()
        except FileNotFoundError:
            return status

        # Check if clamd is running
        try:
            proc = await asyncio.create_subprocess_exec(
                "clamdscan", "--ping", "5",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, _ = await proc.communicate()
            status["daemon_running"] = proc.returncode == 0
        except FileNotFoundError:
            pass

        return status
