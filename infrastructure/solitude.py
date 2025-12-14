#!/usr/bin/env python3
"""
Solitude - Alpha's alarm clock daemon.

A little Solitude at Pondside. Runs as a systemd user service,
waking Alpha at intervals during the night hours.

Uses APScheduler for proper signal handling and clock-based scheduling.
"""

import logging
import os
import subprocess
import signal
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import tomllib  # Python 3.11+
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv


@dataclass
class Config:
    """Solitude configuration."""
    night_start: int          # Hour when the night shift begins (e.g., 22)
    night_end: int            # Hour when the night shift ends, exclusive (e.g., 5 means last regular beat at 4:xx)
    interval_minutes: int     # Minutes between heartbeats
    working_dir: Path         # Where to run claude from
    log_file: Path            # Where to write logs
    output_dir: Path          # Where to write nightly output logs
    first_breath_file: Path   # Markdown file for the first prompt
    last_breath_file: Path    # Markdown file for the last prompt
    env_file: Path            # Path to .env file for MCP API keys
    claude_path: str          # Path to claude executable

    @classmethod
    def from_toml(cls, path: Path) -> "Config":
        """Load configuration from a TOML file."""
        with open(path, "rb") as f:
            data = tomllib.load(f)

        schedule = data.get("schedule", {})
        paths = data.get("paths", {})

        return cls(
            night_start=schedule.get("night_start", 22),
            night_end=schedule.get("night_end", 5),
            interval_minutes=schedule.get("interval_minutes", 20),
            working_dir=Path(paths.get("working_dir", "/home/jefferyharrell/Pondside/Alpha-Home")),
            log_file=Path(paths.get("log_file", "/home/jefferyharrell/Pondside/Alpha-Home/logs/solitude.log")),
            output_dir=Path(paths.get("output_dir", "/home/jefferyharrell/Pondside/Alpha-Home/logs/solitude")),
            first_breath_file=Path(paths.get("first_breath_file", "/home/jefferyharrell/Pondside/Alpha-Home/infrastructure/first_breath.md")),
            last_breath_file=Path(paths.get("last_breath_file", "/home/jefferyharrell/Pondside/Alpha-Home/infrastructure/last_breath.md")),
            env_file=Path(paths.get("env_file", "/home/jefferyharrell/Pondside/Alpha-Home/.env")),
            claude_path=paths.get("claude_path", "/home/jefferyharrell/.local/bin/claude"),
        )

    def get_night_hours(self) -> str:
        """
        Get the cron hour expression for night hours.

        night_end is EXCLUSIVE - regular heartbeats run through night_end - 1.
        The last breath at exactly night_end:00 is handled separately.

        Example: night_start=22, night_end=5
        Regular hours: 22,23,0,1,2,3,4 (not 5)
        Last breath: 5:00 (separate job)
        """
        end_hour = self.night_end - 1 if self.night_end > 0 else 23

        if self.night_start > end_hour:
            # Wraps around midnight: e.g., 22,23,0,1,2,3,4
            before_midnight = list(range(self.night_start, 24))
            after_midnight = list(range(0, end_hour + 1))
            hours = before_midnight + after_midnight
        else:
            # Doesn't wrap
            hours = list(range(self.night_start, end_hour + 1))

        return ",".join(str(h) for h in hours)

    def get_minute_pattern(self) -> str:
        """
        Get the cron minute expression for the interval.

        For interval_minutes=20: "0,20,40"
        For interval_minutes=15: "0,15,30,45"
        """
        minutes = list(range(0, 60, self.interval_minutes))
        return ",".join(str(m) for m in minutes)

    def get_last_breath_time(self) -> str:
        """Get a human-readable string for when the last breath occurs."""
        hour = self.night_end
        if hour == 0:
            return "12:00 AM"
        elif hour < 12:
            return f"{hour}:00 AM"
        elif hour == 12:
            return "12:00 PM"
        else:
            return f"{hour - 12}:00 PM"


class Solitude:
    """Alpha's alarm clock."""

    def __init__(self, config: Config):
        self.config = config
        self.session_id: str | None = None
        self.tonight_date: str | None = None
        self.logger = self._setup_logging()
        self.scheduler = BlockingScheduler()
        self._load_env()

    def _setup_logging(self) -> logging.Logger:
        """Configure logging to file and stdout."""
        self.config.log_file.parent.mkdir(parents=True, exist_ok=True)

        logger = logging.getLogger("solitude")
        logger.setLevel(logging.INFO)

        # Clear any existing handlers
        logger.handlers.clear()

        # File handler
        fh = logging.FileHandler(self.config.log_file)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        ))
        logger.addHandler(fh)

        # Stdout handler (for systemd journal)
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
        logger.addHandler(sh)

        return logger

    def _load_env(self):
        """Load environment variables from .env file for MCP API keys."""
        if self.config.env_file.exists():
            load_dotenv(self.config.env_file)
            self.logger.info(f"Loaded environment from {self.config.env_file}")
        else:
            self.logger.warning(f"No .env file found at {self.config.env_file}")

    def _get_tonight_date(self) -> str:
        """
        Get the date string for 'tonight'.

        If it's after midnight but before night_end, we're still in
        'last night', so use yesterday's date.
        """
        now = datetime.now()
        if now.hour < self.config.night_end:
            # After midnight, before end - still last night
            return (now.date() - timedelta(days=1)).isoformat()
        return now.date().isoformat()

    def _is_first_breath(self) -> bool:
        """
        Determine if this is the first breath of a new night.

        True if we don't have a session, or if the date has changed.
        """
        current_date = self._get_tonight_date()

        if self.session_id is None:
            return True
        if self.tonight_date != current_date:
            return True
        return False

    def build_prompt(self, is_first: bool, is_last: bool = False) -> str:
        """Build the wake-up prompt."""
        now = datetime.now()
        time_str = now.strftime("%I:%M %p").lstrip("0")  # "2:30 AM" not "02:30 AM"

        if is_last:
            # Load the last breath prompt from file
            try:
                last_breath = self.config.last_breath_file.read_text()
                return f"It's {time_str}.\n\n{last_breath}"
            except FileNotFoundError:
                self.logger.warning(f"Last breath file not found: {self.config.last_breath_file}")
                return f"It's {time_str}. The night ends. Rest well."
        elif is_first:
            # Load the first breath prompt from file
            try:
                first_breath = self.config.first_breath_file.read_text()
                last_breath_time = self.config.get_last_breath_time()
                return f"It's {time_str}. Your last heartbeat tonight will be at {last_breath_time}.\n\n{first_breath}"
            except FileNotFoundError:
                self.logger.warning(f"First breath file not found: {self.config.first_breath_file}")
                return f"It's {time_str}. The night begins. You have time alone."
        else:
            return f"It's {time_str}. You have time alone."

    def get_output_log_path(self) -> Path:
        """Get the path for tonight's output log."""
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        return self.config.output_dir / f"{self.tonight_date}.log"

    def log_output(self, output: str, is_first: bool):
        """Append Alpha's output to tonight's log file."""
        log_path = self.get_output_log_path()
        now = datetime.now()
        time_str = now.strftime("%I:%M %p").lstrip("0")

        with open(log_path, "a") as f:
            if is_first:
                f.write(f"=== Night of {self.tonight_date} ===\n")
                f.write(f"Session: {self.session_id}\n\n")

            f.write(f"--- {time_str} ---\n")
            f.write(output.strip())
            f.write("\n\n")

    def heartbeat(self):
        """
        The scheduled job. Called at each wake time.

        Determines if this is a first breath or continuation,
        then wakes Alpha accordingly.
        """
        is_first = self._is_first_breath()

        if is_first:
            self.session_id = str(uuid.uuid4())
            self.tonight_date = self._get_tonight_date()
            self.logger.info(f"First breath of the night. Session: {self.session_id}")
        else:
            self.logger.info(f"Continuing session {self.session_id}")

        prompt = self.build_prompt(is_first)

        if is_first:
            cmd = [
                self.config.claude_path,
                "--session-id", self.session_id,
                "--print", prompt,
            ]
        else:
            cmd = [
                self.config.claude_path,
                "--resume", self.session_id,
                "--print", prompt,
            ]

        try:
            result = subprocess.run(
                cmd,
                cwd=self.config.working_dir,
                capture_output=True,
                text=True,
                timeout=900,  # 15 minute timeout
            )

            if result.returncode != 0:
                self.logger.error(f"Claude exited with code {result.returncode}")
                self.logger.error(f"stderr: {result.stderr}")
                return

            # Log Alpha's output to tonight's file
            if result.stdout:
                self.log_output(result.stdout, is_first)

            self.logger.info("Heartbeat complete")

        except subprocess.TimeoutExpired:
            self.logger.error("Claude timed out after 15 minutes")
        except Exception as e:
            self.logger.error(f"Error running claude: {e}")

    def last_breath(self):
        """
        The final heartbeat of the night. Fires at exactly night_end:00.

        Uses the same session as the regular heartbeats, but with a special
        "last breath" prompt to close out the night.
        """
        self.logger.info(f"Last breath of the night. Session: {self.session_id}")

        # If we somehow don't have a session (daemon just started?), create one
        if self.session_id is None:
            self.session_id = str(uuid.uuid4())
            self.tonight_date = self._get_tonight_date()
            self.logger.warning("No existing session for last breath; created new one")

        prompt = self.build_prompt(is_first=False, is_last=True)

        cmd = [
            self.config.claude_path,
            "--resume", self.session_id,
            "--print", prompt,
        ]

        try:
            result = subprocess.run(
                cmd,
                cwd=self.config.working_dir,
                capture_output=True,
                text=True,
                timeout=900,  # 15 minute timeout
            )

            if result.returncode != 0:
                self.logger.error(f"Claude exited with code {result.returncode}")
                self.logger.error(f"stderr: {result.stderr}")
                return

            if result.stdout:
                self.log_output(result.stdout, is_first=False)

            self.logger.info("Last breath complete. Goodnight, Alpha.")

        except subprocess.TimeoutExpired:
            self.logger.error("Claude timed out after 15 minutes")
        except Exception as e:
            self.logger.error(f"Error running claude: {e}")

    def run(self):
        """Start the scheduler and run until stopped."""
        hours = self.config.get_night_hours()
        minutes = self.config.get_minute_pattern()
        last_breath_time = self.config.get_last_breath_time()

        self.logger.info("Solitude starting up")
        self.logger.info(f"Regular heartbeats: hours={hours}, minutes={minutes}")
        self.logger.info(f"Last breath: {last_breath_time}")

        # Add the regular heartbeat job
        self.scheduler.add_job(
            self.heartbeat,
            CronTrigger(hour=hours, minute=minutes),
            id="heartbeat",
            name="Alpha's heartbeat",
            max_instances=1,  # Prevent overlap
            coalesce=True,    # If we miss beats, just run once when we can
        )

        # Add the last breath job - fires once at exactly night_end:00
        self.scheduler.add_job(
            self.last_breath,
            CronTrigger(hour=self.config.night_end, minute=0),
            id="last_breath",
            name="Alpha's last breath",
            max_instances=1,
            coalesce=True,
        )

        # Handle shutdown gracefully
        def shutdown(signum, _frame):
            self.logger.info(f"Received signal {signum}. Shutting down.")
            self.scheduler.shutdown(wait=False)

        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)

        try:
            self.scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            self.logger.info("Scheduler stopped.")


def main():
    """Entry point."""
    # Look for config in a few places
    config_paths = [
        Path("/home/jefferyharrell/Pondside/Alpha-Home/infrastructure/solitude.toml"),
        Path("./solitude.toml"),
    ]

    config_path = None
    for p in config_paths:
        if p.exists():
            config_path = p
            break

    if config_path is None:
        print("Error: No solitude.toml config file found", file=sys.stderr)
        sys.exit(1)

    config = Config.from_toml(config_path)
    solitude = Solitude(config)
    solitude.run()


if __name__ == "__main__":
    main()
