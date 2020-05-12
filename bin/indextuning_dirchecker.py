from __future__ import print_function
import os
import logging

logger = logging.getLogger()

# check_dirs checks that the directories on the filesystem relate to a real index and have not
# accidentally been left here by indexes that have been deleted
# If they have been left here it suggests a list of directories that could be deleted
def check_for_dead_dirs(index_list, vol_list, excluded_dirs, utility):

    index_dirs_to_check_hot = {}
    index_dirs_to_check_cold = {}
    summary_dirs_to_check = {}
    index_dirs_to_check_thawed = {}

    # Splunk uses the $SPLUNK_DB variable to specify the default location of the data
    splunkDBLoc = os.environ['SPLUNK_DB']
    for index in index_list:
        # expecting something similar to
        # home_path = volume:hot/$_index_name/db
        # cold_path = volume:cold/$_index_name/colddb
        # tstats_home_path = volume:_splunk_summaries/$_index_name/datamodel_summary
        home_path = index_list[index].home_path
        cold_path = index_list[index].cold_path
        tstats_home_path = index_list[index].tstats_home_path
        thawed_path = index_list[index].thawed_path
        if hasattr(index_list[index], "cold_to_frozen_dir"):
            cold_to_frozen_dir = index_list[index].cold_to_frozen_dir
        else:
            cold_to_frozen_dir = False

        logger.debug("dead dirs prechanges index=%s home_path=%s cold_path=%s tstats_home_path=%s thawed_path=%s cold_to_frozen_dir=%s" % (index, home_path, cold_path, tstats_home_path, thawed_path, cold_to_frozen_dir))
        # Ok we found a volume, replace it with the full directory path for the dir function to work
        if (home_path.find("volume:") != -1):
            end = home_path.find("/")
            findVol = home_path[7:end]
            home_path = home_path.replace("volume:%s" % (findVol), vol_list[findVol].path)

        # Ok we found a volume, replace it with the full directory path for the dir function to work
        if (cold_path.find("volume:") != -1):
            end = cold_path.find("/")
            findVol = cold_path[7:end]
            cold_path = cold_path.replace("volume:%s" % (findVol), vol_list[findVol].path)

        # Ok we found a volume, replace it with the full directory path for the dir function to work
        if (tstats_home_path.find("volume:") != -1):
            end = tstats_home_path.find("/")
            findVol = tstats_home_path[7:end]
            tstats_home_path = tstats_home_path.replace("volume:%s" % (findVol), vol_list[findVol].path)

        home_path = home_path.replace("$SPLUNK_DB", splunkDBLoc)
        cold_path = cold_path.replace("$SPLUNK_DB", splunkDBLoc)
        tstats_home_path = tstats_home_path.replace("$SPLUNK_DB", splunkDBLoc)
        thawed_path = thawed_path.replace("$SPLUNK_DB", splunkDBLoc)

        # $_index_name is just a variable for the index name stanza
        home_path = home_path.replace("$_index_name", index).replace("//","/").lower()
        cold_path = cold_path.replace("$_index_name", index).replace("//","/").lower()
        tstats_home_path = tstats_home_path.replace("$_index_name", index).replace("//","/").lower()
        thawed_path = thawed_path.replace("$_index_name", index).replace("//","/").lower()

        # Splunk changes any directory specified in mixed case for home_path/cold_path/tstats_home_path locations to lowercase
        # btool does not therefore we lower() here
        index_list[index].home_path = home_path
        index_list[index].cold_path = cold_path
        index_list[index].tstats_home_path = tstats_home_path
        index_list[index].thawed_path = thawed_path

        # Drop off the /db/, /cold_path, or /datamodel directories off the end of, for example /opt/splunk/var/lib/splunk/_internaldb/db
        home_path = home_path[:home_path.rfind("/")]
        cold_path = cold_path[:cold_path.rfind("/")]
        tstats_home_path = tstats_home_path[:tstats_home_path.rfind("/")]
        thawed_path = thawed_path[:thawed_path.rfind("/")]
        if hasattr(index_list[index], "cold_to_frozen_dir"):
            cold_to_frozen_dir = cold_to_frozen_dir[:cold_to_frozen_dir.rfind("/")]
        else:
            cold_to_frozen_dir = False

        # drop off the /<index dir name> off the end, for example /opt/splunk/var/lib/splunk/_internaldb
        # this leaves a top level directory such as /opt/splunk/var/lib/splunk
        home_path_dir = home_path[:home_path.rfind("/")]
        cold_path_dir = cold_path[:cold_path.rfind("/")]
        tstats_home_path_dir = tstats_home_path[:tstats_home_path.rfind("/")]
        thawed_path_dir = thawed_path[:thawed_path.rfind("/")]

        # keep the dictionary up-to-date with directories that must be checked
        index_dirs_to_check_hot[home_path_dir] = True
        index_dirs_to_check_cold[cold_path_dir] = True
        summary_dirs_to_check[tstats_home_path_dir] = True
        index_dirs_to_check_thawed[thawed_path_dir] = True

        logger.debug("dead dirs postchanges index=%s home_path=%s cold_path=%s tstats_home_path=%s thawed_path=%s cold_to_frozen_dir=%s" % (index, home_path, cold_path, tstats_home_path, thawed_path, cold_to_frozen_dir))

    # At this point we know what indexes we need to check
    dead_index_dir_list_hot = check_dirs(index_list, index_dirs_to_check_hot, excluded_dirs, utility)
    dead_index_dir_list_cold = check_dirs(index_list, index_dirs_to_check_cold, excluded_dirs, utility)
    dead_index_dir_list_summaries = check_dirs(index_list, summary_dirs_to_check, excluded_dirs, utility)
    dead_index_dir_list_thawed = check_dirs(index_list, index_dirs_to_check_thawed, excluded_dirs, utility)

    logger.debug("Returning these lists to be checked: dead_index_dir_list_hot=\"%s\", dead_index_dir_list_cold=\"%s\", dead_index_dir_list_summaries=\"%s\", dead_index_dir_list_thawed=\"%s\""
                  % (dead_index_dir_list_hot, dead_index_dir_list_cold, dead_index_dir_list_summaries, dead_index_dir_list_thawed))
    return { "hot_dirs_checked" : index_dirs_to_check_hot, "hot_dirs_dead": dead_index_dir_list_hot, "cold_dirs_checked" : index_dirs_to_check_cold,
    "cold_dirs_dead" : dead_index_dir_list_cold, "summaries_dirs_checked" : summary_dirs_to_check, "summaries_dirs_dead" : dead_index_dir_list_summaries,
    "thawed_dirs_checked" : dead_index_dir_list_thawed }

def check_dirs(index_list, dirsToCheck, excluded_dirs, utility):
    dead_dir_list = {}
    # For each directory that we should be checking we check if we have an index that relates to the sub-directories, if not it's probably an old directory
    # left around by an index that has been removed from the config but left on the filesystem
    for dirs in list(dirsToCheck.keys()):
        # list the directories we see under the specified paths, ignoring files
        logger.debug("Now checking directory=%s" % (dirs))
        try:
            dirlist = utility.listdirs(dirs)
        except OSError as e:
            if e.strerror.find("No such file or directory") != -1:
                print(e)

        for dir in dirlist:
            found = False
            logger.debug("Checking subdir=%s of dir=%s" % (dir, dirs))
            # If we cannot find any mention of this index name then most likely it exists from a previous config / needs cleanup
            abs_dir = dirs + "/" + dir
            for index in index_list:
                home_path = index_list[index].home_path
                home_path = home_path[:home_path.rfind("/")]
                cold_path = index_list[index].cold_path
                cold_path = cold_path[:cold_path.rfind("/")]
                tstats_home_path = index_list[index].tstats_home_path
                tstats_home_path = tstats_home_path[:tstats_home_path.rfind("/")]
                thawed_path = index_list[index].thawed_path
                thawed_path = thawed_path[:thawed_path.rfind("/")]
                if hasattr(index_list[index], "cold_to_frozen_dir"):
                    cold_to_frozen_dir = index_list[index].cold_to_frozen_dir
                    cold_to_frozen_dir2 = index_list[index].cold_to_frozen_dir
                    cold_to_frozen_dir = cold_to_frozen_dir[:cold_to_frozen_dir.rfind("/")]
                else:
                    cold_to_frozen_dir = False
                    cold_to_frozen_dir2 = False

                # logger.debug("home path is %s" % (home_path))
                if abs_dir==home_path or abs_dir==cold_path or abs_dir==tstats_home_path or abs_dir==thawed_path or abs_dir==cold_to_frozen_dir:
                    found = True
                    break
                else:
                    # don't include the excluded directories
                    if dir in excluded_dirs:
                        logger.debug("dir=%s is excluded so marking it found" % (dir))
                        found = True
                        break
            if not found:
                logger.debug("dir=%s not found in the btool listing for splunk btool indexes list --debug" % (dir))
                # If someone created the $_index_name on the filesystem...
                dead_dir = utility.replace_dollar_symbols(dir)
                if not dead_dir in dead_dir_list:
                    dead_dir_list[dead_dir] = []
                dead_dir_list[dead_dir].append(dirs)
                logger.debug("dir=%s appears to be unused, adding to the list to be removed" % (dead_dir))

            try:
                sub_dir_list = utility.listdirs(abs_dir)
                logger.debug("Working with sub_dirs=\"%s\" from abs_dir=%s" % (sub_dir_list, abs_dir))
            except OSError as e:
                if e.strerror.find("No such file or directory") != -1:
                    logger.error(e)
                continue

            found2 = False
            for a_dir in sub_dir_list:
                #These are always excluded as they should never be deleted
                if dir in ["fishbucket", "main", "$_index_name", "kvstore", "persistentstorage"]:
                    continue
                abs_dir2 = abs_dir + "/" + a_dir
                for index in index_list:
                    if abs_dir2==index_list[index].home_path or abs_dir2==index_list[index].cold_path or abs_dir2==index_list[index].tstats_home_path \
                    or abs_dir2==index_list[index].thawed_path or abs_dir2==cold_to_frozen_dir2:
                        found2 = True
                        break
                if not found2:
                    logger.debug("dir=%s not found in the btool listing for splunk btool indexes list --debug / home_path's" % (abs_dir2))
                    #If someone created the $_index_name on the filesystem...
                    dead_dir = utility.replace_dollar_symbols(dir + "/" + a_dir)
                    if not dead_dir in dead_dir_list:
                        dead_dir_list[dead_dir] = []
                    dead_dir_list[dead_dir].append(dirs)
                    logger.debug("dir=%s appears to be unused, adding to the list" % (dead_dir))

    return dead_dir_list
