import indextuning_utility_k8s as idx_utility
import os
import shutil
import logging
from pathlib import Path
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
import sys

log_file = "/opt/splunk/var/log/splunk/dead_index_cleaner.log"

# Create rotating file handler
file_handler = RotatingFileHandler(
    log_file,
    maxBytes=1 * 1024 * 1024,  # 1 MB
    backupCount=5              # Keep 5 rotated files
)

file_handler.setFormatter(
    logging.Formatter("%(asctime)s %(levelname)s %(message)s")
)

# Configure root logger
logging.basicConfig(
    level=logging.INFO,
    handlers=[file_handler]
)

utility = idx_utility.utility()

base_dir = Path("/opt/splunk/var/lib/splunk")

logging.info("Begin script")

# Determine the index/volume list we are working with
index_list, vol_list = utility.parse_btool_output()
if len(index_list) < 50:
    logging.warning(f"Less than 50 indexes found. count={len(index_list)} exiting the script as this might be invalid output")
    sys.exit(1)

# List of files/directories that must be retained
keep_list = {
    "_internaldb",
    "_introspection",
    "_telemetry",
    "authDb",
    "defaultdb",
    "db",
    "kvstore",
    "fishbucket",
    "licenses",
    ".snapshots",
    "summarydb",
    ".dirty_database",
    "persistentstorage",
    "hashDb",
    "modinputs"
}

for index in index_list:
   home_path = index_list[index].home_path
   if "$SPLUNK_DB" in home_path:
       home_path = home_path.replace("$SPLUNK_DB/","")
       home_path = home_path.replace("$_index_name", index)
   thawed_path = index_list[index].thawed_path
   home_path = home_path.replace("/db","").lower()
   thawed_path = thawed_path.replace("/thaweddb","")
   thawed_path = thawed_path.replace("$SPLUNK_DB/","")
   thawed_path = thawed_path.replace("/opt/splunk/var/lib/splunk/","")
   summary_path = index_list[index].tstats_home_path
   summary_path = summary_path.replace("volume:_splunk_summaries/","")
   summary_path = summary_path.replace("$_index_name", index)
   summary_path = summary_path[0:summary_path.rfind("/")]
   logging.debug(f"Adding {index} to keep list, along with path {home_path} and thawed_path={thawed_path}")
   keep_list.add(home_path)
   # thawed path can use mixed case, all others default to lowercase
   keep_list.add(thawed_path)
   keep_list.add(index.lower() + ".dat")
   keep_list.add(summary_path)

AGE_DAYS = 30
cutoff_time = datetime.now() - timedelta(days=AGE_DAYS)

def main():
    if not base_dir.is_dir():
        logging.error("Directory does not exist: %s", base_dir)
        return 1

    for entry in base_dir.iterdir():
        entry_name = entry.name

        if entry_name in keep_list:
            logging.debug(f"Keeping entry={entry}")
            continue

        try:
            # Check all subdirectories under entry
            if entry.is_dir():
                skip_dir = False

                for subdir in entry.iterdir():
                    if not subdir.is_dir():
                        continue

                    subdir_mtime = datetime.fromtimestamp(subdir.stat().st_mtime)
                    subdir_age_days = (datetime.now() - subdir_mtime).days

                    if subdir_mtime > cutoff_time:
                        logging.info(
                            "Skipping dir=%s because subdir=%s was modified %d days ago",
                            entry,
                            subdir,
                            subdir_age_days,
                        )
                        skip_dir = True
                        break

                if skip_dir:
                    continue

            # Get modification time of the entry itself
            mtime = datetime.fromtimestamp(entry.stat().st_mtime)
            age_days = (datetime.now() - mtime).days

            if mtime > cutoff_time:
                logging.info(
                    "Skipping entry=%s (only %d days old, must be >= %d days)",
                    entry,
                    age_days,
                    AGE_DAYS,
                )
                continue

            if entry.is_dir():
                logging.warning(
                    "Deleting dir=%s (last modified=%s, age=%d days)",
                    entry,
                    mtime.strftime("%Y-%m-%d %H:%M:%S"),
                    age_days,
                )
                shutil.rmtree(entry)
            else:
                logging.warning(
                    "Deleting file=%s (last modified=%s, age=%d days)",
                    entry,
                    mtime.strftime("%Y-%m-%d %H:%M:%S"),
                    age_days,
                )
                entry.unlink()

            logging.info("Deleted=%s", entry)

        except Exception as e:
            logging.error("Failed to delete %s: %s", entry, e)

    logging.info("Cleanup complete")
    return 0

main()

