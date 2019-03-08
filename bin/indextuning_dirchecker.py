import os
import logging

#checkDirs checks that the directories on the filesystem relate to a real index and have not
#accidentally been left here by indexes that have been deleted
#If they have been left here it suggests a list of directories that could be deleted
def checkForDeadDirs(indexList, volList, excludedDirs, utility, logging):
    logging = logging.getLogger("dirchecker")

    indexDirsToCheckHot = {}
    indexDirsToCheckCold = {}
    summaryDirsToCheck = {}

    #Splunk uses the $SPLUNK_DB variable to specify the default location of the data
    splunkDBLoc = os.environ['SPLUNK_DB']
    for index in indexList:
        #expecting something similar to
        #homePath = volume:hot/$_index_name/db
        #coldPath = volume:cold/$_index_name/colddb
        #tstatsHomePath = volume:_splunk_summaries/$_index_name/datamodel_summary
        homePath = indexList[index]["homePath"]
        coldPath = indexList[index]["coldPath"]
        tstatsHomePath = indexList[index]["tstatsHomePath"]

        #Ok we found a volume, replace it with the full directory path for the dir function to work
        if (homePath.find("volume:") != -1):
            end = homePath.find("/")
            findVol = homePath[7:end]
            homePath = homePath.replace("volume:%s" % (findVol), volList[findVol]["path"])

        #Ok we found a volume, replace it with the full directory path for the dir function to work
        if (coldPath.find("volume:") != -1):
            end = coldPath.find("/")
            findVol = coldPath[7:end]
            coldPath = coldPath.replace("volume:%s" % (findVol), volList[findVol]["path"])

        #Ok we found a volume, replace it with the full directory path for the dir function to work
        if (tstatsHomePath.find("volume:") != -1):
            end = tstatsHomePath.find("/")
            findVol = tstatsHomePath[7:end]
            tstatsHomePath = tstatsHomePath.replace("volume:%s" % (findVol), volList[findVol]["path"])

        homePath = homePath.replace("$SPLUNK_DB", splunkDBLoc)
        coldPath = coldPath.replace("$SPLUNK_DB", splunkDBLoc)
        tstatsHomePath = tstatsHomePath.replace("$SPLUNK_DB", splunkDBLoc)

        #$_index_name is just a variable for the index name stanza
        homePath = homePath.replace("$_index_name", index)
        coldPath = coldPath.replace("$_index_name", index)
        tstatsHomePath = tstatsHomePath.replace("$_index_name", index)

        #Drop off the /db/, /coldpath, or /datamodel directories off the end of, for example /opt/splunk/var/lib/splunk/_internaldb/db
        homePath = homePath[:homePath.rfind("/")]
        coldPath = coldPath[:coldPath.rfind("/")]
        tstatsHomePath = tstatsHomePath[:tstatsHomePath.rfind("/")]

        #Splunk changes any directory specified in mixed case for homePath/coldPath/tstatsHomePath locations to lowercase
        #btool does not therefore we lower() here
        indexList[index]["homePath"] = homePath.lower()
        indexList[index]["coldPath"] = coldPath.lower()
        indexList[index]["tstatsHomePath"] = tstatsHomePath.lower()

        #drop off the /<index dir name> off the end, for example /opt/splunk/var/lib/splunk/_internaldb
        #this leave a top level directory such as /opt/splunk/var/lib/splunk
        homePathDir = homePath[:homePath.rfind("/")]
        coldPathDir = coldPath[:coldPath.rfind("/")]
        tstatsHomePathDir = tstatsHomePath[:tstatsHomePath.rfind("/")]

        #keep the dictionary up-to-date with directories that must be checked
        indexDirsToCheckHot[homePathDir] = True
        indexDirsToCheckCold[coldPathDir] = True
        summaryDirsToCheck[tstatsHomePathDir] = True

        logging.debug("now : %s %s %s %s" % (index, homePath, coldPath, tstatsHomePath))

    #At this point we know what indexes we need to check
    deadIndexDirListHot = checkDirs(indexList, indexDirsToCheckHot, excludedDirs, utility, logging)
    deadIndexDirListCold = checkDirs(indexList, indexDirsToCheckCold, excludedDirs, utility, logging)
    deadIndexDirListSummaries = checkDirs(indexList, summaryDirsToCheck, excludedDirs, utility, logging)
    
    return { "hotDirsChecked" : indexDirsToCheckHot, "hotDirsDead": deadIndexDirListHot, "coldDirsChecked" : indexDirsToCheckCold, "coldDirsDead" : deadIndexDirListCold, "summariesDirsChecked" : summaryDirsToCheck, "summariesDirsDead" : deadIndexDirListSummaries }
    
def checkDirs(indexList, dirsToCheck, excludedDirs, utility, logging):
    deadDirList = {}
    #For each directory that we should be checking we check if we have an index that relates to the sub-directories, if not it's probably an old directory
    #left around by an index that has been removed from the config but left on the filesystem
    for dirs in dirsToCheck.keys():
        #list the directories we see under the specified paths, ignoring files
        logging.debug("Now checking directory %s" % (dirs))
        dirlist = utility.listdirs(dirs) 
        for dir in dirlist:
            found = False
            logging.debug("Checking subdir %s of dir %s" % (dir, dirs))
            #If we cannot find any mention of this index name then most likely it exists from a previous config / needs cleanup
            for index in indexList:
                if dir in indexList[index]["homePath"] or dir in indexList[index]["coldPath"] or dir in indexList[index]["tstatsHomePath"]:
                    found = True
                    break
                else:
                    #don't include the excluded directories
                    if (dir in excludedDirs):
                        found = True
                        break
            if not found:
                logging.debug("Directory name %s not found in the btool listing for splunk btool indexes list --debug / homePath's" % (dir))
                #If someone created the $_index_name on the filesystem...
                dir = utility.replaceDollarSymbols(dir)
                if not dir in deadDirList:
                    deadDirList[dir] = []
                deadDirList[dir].append(dirs)
                logging.debug("(Index) Directory %s appears to be unused, adding to the list" % (dir))    
    return deadDirList