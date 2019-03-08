import os
import re

###############################
#
# Begin index file re-writing into temp directory
#   This function exists to read a (real) indexes.conf file from the filesystem and to change and/or add any lines that 
#   we require into the indexes.conf file
#   We write that out to a new directory so that we can run differencing between existing and new file
#
###############################
def outputIndexFilesIntoTemp(logging, confFilesRequiringChanges, indexList, path, indexesRequiringChanges, replaceSlashes=True):
    #Create the required directory
    try:
        os.mkdir(path, 0o750)
    except OSError:
        #Debug level because this directory may already exist after previous runs
        logging.debug("Creation of the directory %s failed" % path)

    #At this point we have a list of files requiring changes, a list of indexes with that file that require changing
    #we now read through the file and output an equivalent file in the working path that is the tuned version
    #we can (outside this script) diff the 2 files and / or implement the new file as required on the cluster master
    regex = re.compile("^\s*([^= ]+)")
    
    for aFile in confFilesRequiringChanges:
        with open(aFile) as file:
            #TODO find a nicer way to do this
            #there is no obvious way to determine the end of an index stanza entry or any stanza entry in the indexes.conf file, therefore we know
            #that we have finished the stanza entry when we have either reached a new entry or the end of the file
            #however that means we'd have [indexxxx]...\n<blank line>\n<insert our line here>\n[nextindexyyy]...
            #to ensure we have [indexxxx]...\n<insert our line here>\n[nextindexyyy]...the script prints 2 lines behind to the file...
            previousLine = False
            prevPreviousLine = False
            indexName = ""
            changesRequired = False
            maxDataSizeDone = False
            maxTotalDataSizeDone = False 

            #name the output file based on the location on disk of the conf file
            #which means we replace / with _ symbols
            if replaceSlashes:
                outputFile = aFile[aFile.find("slave-apps"):].replace("/", "_")
            else:
                outputFile = aFile[aFile.find("slave-apps")+11:]
            #output a new file in the working directory with our tuning modifications
            outputH = open(path + "/" + outputFile, "w")

            for line in file:
                if (prevPreviousLine):
                    outputH.write(prevPreviousLine)

                #We found a stanza
                if (line.find("[") == 0):
                    #We don't need to do much with a volume stanza
                    if (line.find("[volume:") == -1):
                        #We have moved onto a new index entry, but did we finish our previous job?
                        #It's possible that maxTotalDataSizeMB was never specified in the stanza as it's optional
                        #therefore we now write it out
                        if (changesRequired != False):
                            outputEdgeCase(logging, changesRequired, indexList, maxDataSizeDone, maxTotalDataSizeDone, outputH, indexName)
                        
                        #Some items are written into every index entry such as maxDataSize and maxTotalDataSize
                        maxDataSizeDone = False
                        maxTotalDataSizeDone = False
        
                        end = line.find("]") 
                        indexName = line[1:end]
                        if (indexesRequiringChanges.has_key(indexName) and indexList[indexName].has_key("checked")):
                            changesRequired = indexesRequiringChanges[indexName].split("_")
                            logging.debug("index list info: %s" % (indexList[indexName]))
                        else:
                            changesRequired = False
                    else:
                        changesRequired = False
        
                #We are somewhere after the [index...] stanza
                if (changesRequired != False):
                    result = regex.match(line)
                    stanza = result.group(1)
        
                    #If we have changes and we come across the stanza that requires changes, write it out, potentially with a comment we created earlier
                    if (("bucket" in changesRequired) and stanza == "maxDataSize"):
                        recBucketSize = indexList[indexName]['recBucketSize']
                        comment = indexList[indexName]['changeComment']['bucket']
                        #strip off the newline character from the line before adding to the log, otherwise the log has random newlines in it
                        logging.debug("Old line %s, new line %s (newline) maxDataSize = %s" % (line[:-1], comment[:-1], recBucketSize))
                        #overwrite the old line with the new one
                        line = "%smaxDataSize = %s\n" % (comment, recBucketSize)
                        maxDataSizeDone = True
                    elif (("sizing" in changesRequired) and stanza == "maxTotalDataSizeMB"):
                        calcMaxTotalDataSizeMB = indexList[indexName]['calcMaxTotalDataSizeMB']
                        comment = indexList[indexName]['changeComment']['sizing']
                        #strip off the newline character from the line before adding to the log
                        logging.debug("Old line %s, new line of %s (newline) maxTotalDataSizeMB = %s" % (line[:-1], comment[:-1], calcMaxTotalDataSizeMB))
                        line = "%smaxTotalDataSizeMB = %s\n" % (comment, calcMaxTotalDataSizeMB)
                        maxTotalDataSizeDone = True
                    elif (("sizing" in changesRequired) and stanza == "homePath.maxDataSizeMB"):
                        homePathMaxDataSizeMB = indexList[indexName]['homePathMaxDataSizeMB']
                        #strip off the newline character from the line before adding to the log
                        logging.debug("Old line %s, new line homePath.maxDataSizeMB = %s" % (line[:-1], homePathMaxDataSizeMB))
                        line = "homePath.maxDataSizeMB = %s\n" % (homePathMaxDataSizeMB)
                    elif  (("sizing" in changesRequired) and stanza == "coldPath.maxDataSizeMB"):
                        coldPathMaxDataSizeMB = indexList[indexName]['coldPathMaxDataSizeMB']
                        #strip off the newline character from the line before adding to the log
                        logging.debug("Old line %s, new line coldPath.maxDataSizeMB = %s" % (line[:-1], coldPathMaxDataSizeMB)) 
                        line = "coldPath.maxDataSizeMB = %s\n" % (coldPathMaxDataSizeMB)
                #record the previous, previous line if we have recorded a previous line already
                if (previousLine):
                    prevPreviousLine = previousLine
                previousLine = line
        
            #This is an edge case but what if changes required and they were not done already
            #and we hit the end of the file?
            #Then we print out all the required information now
            if (changesRequired == False):
                pass
            else:
                outputEdgeCase(logging, changesRequired, indexList, maxDataSizeDone, maxTotalDataSizeDone, outputH, indexName)
                
            #print out the remaining lines
            outputH.write(prevPreviousLine)
            outputH.write(previousLine)

#After we get to a new index entry we might have missed stanzas from the last index entry we were working on
#add them to the output file now
def outputEdgeCase(logging, changesRequired, indexList, maxDataSizeDone, maxTotalDataSizeDone, outputH, indexName):
    if ("bucket" in changesRequired and not "sizing" in changesRequired and not maxDataSizeDone):
        recBucketSize = indexList[indexName]['recBucketSize']
        comment = indexList[indexName]['changeComment']['bucket']
        logging.debug("Never found this so writing it now %s (newline) maxDataSize = %s with a preceding comment of %s" % (comment[:-1], recBucketSize))
        #Write the comment before the bucket sizing, so we record why this was changed
        outputH.write(comment)
        outputH.write("maxDataSize = %s\n" % (recBucketSize))
    elif ("sizing" in changesRequired and not "bucket" in changesRequired and not maxTotalDataSizeDone):
        calcMaxTotalDataSizeMB = indexList[indexName]['calcMaxTotalDataSizeMB']
        comment = indexList[indexName]['changeComment']['sizing']
        outputH.write(comment)
        logging.debug("Never found this so writing it now %s (newline) maxTotalDataSizeMB = %s" % (comment[:-1], calcMaxTotalDataSizeMB))
        outputH.write("maxTotalDataSizeMB = %s\n" % (calcMaxTotalDataSizeMB))
    elif ("bucket" in changesRequired and "sizing" in changesRequired):
        recBucketSize = indexList[indexName]['recBucketSize']
        calcMaxTotalDataSizeMB = indexList[indexName]['calcMaxTotalDataSizeMB']
        
        #If we have not yet written the maxDataSize or maxTotalDataSize entries we write them together
        if (not maxDataSizeDone):
            comment = indexList[indexName]['changeComment']['bucket']
            logging.debug("Never found this so writing it now %s (newline) maxDataSize = %s" % (comment[:-1], recBucketSize))
            outputH.write(comment)
            outputH.write("maxDataSize = %s\n" % (recBucketSize))
        if (not maxTotalDataSizeDone):
            comment = indexList[indexName]['changeComment']['sizing']
            logging.debug("Never found this so writing it now %s (newline) maxTotalDataSizeMB = %s" % (comment[:-1], calcMaxTotalDataSizeMB))
            outputH.write(comment)
            outputH.write("maxTotalDataSizeMB = %s\n" % (calcMaxTotalDataSizeMB))
    #If we have a sizing comment to add and it was not added, do it now...
    if (changesRequired != False and "sizingcomment" in changesRequired):
        comment = indexList[indexName]['changeComment']['sizingcomment']
        outputH.write(comment)
        logging.debug("Wrote the sizing comment as %s" % (comment[:-1]))
