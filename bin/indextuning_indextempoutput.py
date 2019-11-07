import os
import re
from io import open
import logging
import six

logger = logging.getLogger()

###############################
#
# Begin index file re-writing into temp directory
#   This function exists to read a (real) indexes.conf file from the filesystem and to change and/or add any lines that
#   we require into the indexes.conf file
#   We write that out to a new directory so that we can run differencing between existing and new file
#
###############################
def output_index_files_into_temp_dir(conf_files_requiring_change, index_list, path, indexes_requiring_changes, replace_slashes=True):
    #Create the required directory
    try:
        os.mkdir(path, 0o750)
    except OSError:
        #Debug level because this directory may already exist after previous runs
        logger.debug("Creation of the dir=%s failed" % path)

    # At this point we have a list of files requiring changes, a list of indexes with that file that require changing
    # we now read through the file and output an equivalent file in the working path that is the tuned version
    # we can (outside this script) diff the 2 files and / or implement the new file as required on the cluster master
    regex = re.compile(r"^\s*([^= ]+)")

    for a_file in conf_files_requiring_change:
        with open(a_file) as file:
            # TODO find a nicer way to do this
            # there is no obvious way to determine the end of an index stanza entry or any stanza entry in the indexes.conf file, therefore we know
            # that we have finished the stanza entry when we have either reached a new entry or the end of the file
            # however that means we'd have [indexxxx]...\n<blank line>\n<insert our line here>\n[nextindexyyy]...
            # to ensure we have [indexxxx]...\n<insert our line here>\n[nextindexyyy]...the script prints 2 lines behind to the file...
            previous_line = False
            prev_previous_line = False
            index_name = ""
            changes_required = False
            max_data_size_done = False
            max_total_data_size_done = False

            # name the output file based on the location on disk of the conf file
            # which means we replace / with _ symbols
            if replace_slashes:
                output_file = a_file[a_file.find("slave-apps"):].replace("/", "_")
            else:
                output_file = a_file[a_file.find("slave-apps")+11:]
            # output a new file in the working directory with our tuning modifications
            output_h = open(path + "/" + output_file, "w")

            for line in file:
                logger.debug("Working with line: %s" % (line))
                if (prev_previous_line):
                    output_h.write(six.text_type(prev_previous_line))

                # We found a stanza
                if (line.find("[") == 0):
                    # We don't need to do much with a volume stanza
                    if (line.find("[volume:") == -1):
                        # We have moved onto a new index entry, but did we finish our previous job?
                        # It's possible that maxTotalDataSizeMB was never specified in the stanza as it's optional
                        # therefore we now write it out
                        if (changes_required != False):
                            output_edge_case(changes_required, index_list, max_data_size_done, max_total_data_size_done, output_h, index_name)

                        # Some items are written into every index entry such as maxDataSize and maxTotalDataSize
                        max_data_size_done = False
                        max_total_data_size_done = False

                        end = line.find("]")
                        index_name = line[1:end]
                        if (index_name in indexes_requiring_changes and index_list[index_name].checked):
                            changes_required = indexes_requiring_changes[index_name].split("_")
                            logger.debug("index list info=\"%s\"" % (index_list[index_name]))
                        else:
                            changes_required = False
                    else:
                        changes_required = False

                # We are somewhere after the [index...] stanza
                if (changes_required != False):
                    result = regex.match(line)
                    stanza = result.group(1)

                    # If we have changes and we come across the stanza that requires changes, write it out, potentially with a comment we created earlier
                    if (("bucket" in changes_required) and stanza == "maxDataSize"):
                        recommended_bucket_size = index_list[index_name].recommended_bucket_size
                        comment = index_list[index_name].change_comment['bucket']
                        #strip off the newline character from the line before adding to the log, otherwise the log has random newlines in it
                        logger.debug("old_line=%s, new_line=%s (newline) maxDataSize=%s" % (line[:-1], comment[:-1], recommended_bucket_size))
                        #overwrite the old line with the new one
                        line = "%smaxDataSize = %s\n" % (comment, recommended_bucket_size)
                        max_data_size_done = True
                    elif (("sizing" in changes_required) and stanza == "maxTotalDataSizeMB"):
                        calc_max_total_data_size_mb = index_list[index_name].calc_max_total_data_size_mb
                        comment = index_list[index_name].change_comment['sizing']
                        # strip off the newline character from the line before adding to the log
                        logger.debug("old_line=%s, new_line=%s (newline) maxTotalDataSizeMB=%s" % (line[:-1], comment[:-1], calc_max_total_data_size_mb))
                        line = "%smaxTotalDataSizeMB = %s\n" % (comment, calc_max_total_data_size_mb)
                        max_total_data_size_done = True
                    elif (("sizing" in changes_required) and stanza == "homePath.maxDataSizeMB"):
                        homepath_max_data_size_mb = index_list[index_name].homepath_max_data_size_mb
                        # strip off the newline character from the line before adding to the log
                        logger.debug("old_line=%s, new_line=\"homePath.maxDataSizeMB=%s\"" % (line[:-1], homepath_max_data_size_mb))
                        line = "homePath.maxDataSizeMB = %s\n" % (homepath_max_data_size_mb)
                    elif  (("sizing" in changes_required) and stanza == "coldPath.maxDataSizeMB"):
                        coldpath_max_datasize_mb = index_list[index_name].coldpath_max_datasize_mb
                        # strip off the newline character from the line before adding to the log
                        logger.debug("old_line %s, new_line=\"coldPath.maxDataSizeMB=%s\"" % (line[:-1], coldpath_max_datasize_mb))
                        line = "coldPath.maxDataSizeMB = %s\n" % (coldpath_max_datasize_mb)
                # record the previous, previous line if we have recorded a previous line already
                if (previous_line):
                    prev_previous_line = previous_line
                previous_line = line

            # This is an edge case but what if changes required and they were not done already
            # and we hit the end of the file?
            # Then we print out all the required information now
            if (changes_required == False):
                pass
            else:
                output_edge_case(changes_required, index_list, max_data_size_done, max_total_data_size_done, output_h, index_name)

            # print out the remaining lines
            output_h.write(six.text_type(prev_previous_line))
            output_h.write(six.text_type(previous_line))

# After we get to a new index entry we might have missed stanzas from the last index entry we were working on
# add them to the output file now
def output_edge_case(changes_required, index_list, max_data_size_done, max_total_data_size_done, output_h, index_name):
    if ("bucket" in changes_required and not "sizing" in changes_required and not max_data_size_done):
        recommended_bucket_size = index_list[index_name].recommended_bucket_size
        comment = index_list[index_name].change_comment['bucket']
        logger.debug("Never found this so writing it now line=\"%s\" (newline) line=\"maxDataSize=%s\" with a preceding comment=\"%s\"" % (comment, recommended_bucket_size, comment[:-1]))
        #Write the comment before the bucket sizing, so we record why this was changed
        output_h.write(six.text_type(comment))
        output_h.write(six.text_type("maxDataSize = %s\n" % (recommended_bucket_size)))
    elif ("sizing" in changes_required and not "bucket" in changes_required and not max_total_data_size_done):
        calc_max_total_data_size_mb = index_list[index_name].calc_max_total_data_size_mb
        comment = index_list[index_name].change_comment['sizing']
        output_h.write(six.text_type(comment))
        logger.debug("Never found this so writing it now line=\"%s\" (newline) line=\"maxTotalDataSizeMB=%s\"" % (comment[:-1], calc_max_total_data_size_mb))
        output_h.write(six.text_type("maxTotalDataSizeMB = %s\n" % (calc_max_total_data_size_mb)))
    elif ("bucket" in changes_required and "sizing" in changes_required):
        recommended_bucket_size = index_list[index_name].recommended_bucket_size
        calc_max_total_data_size_mb = index_list[index_name].calc_max_total_data_size_mb

        # If we have not yet written the maxDataSize or maxTotalDataSize entries we write them together
        if (not max_data_size_done):
            comment = index_list[index_name].change_comment['bucket']
            logger.debug("Never found this so writing it now line=\"%s\" (newline) line=\"maxDataSize=%s\"" % (comment[:-1], recommended_bucket_size))
            output_h.write(six.text_type(comment))
            output_h.write(six.text_type("maxDataSize = %s\n" % (recommended_bucket_size)))
        if (not max_total_data_size_done):
            comment = index_list[index_name].change_comment['sizing']
            logger.debug("Never found this so writing it now line=\"%s\" (newline) line=\"maxTotalDataSizeMB=%s\"" % (comment[:-1], calc_max_total_data_size_mb))
            output_h.write(six.text_type(comment))
            output_h.write(six.text_type("maxTotalDataSizeMB = %s\n" % (calc_max_total_data_size_mb)))
    #If we have a sizing comment to add and it was not added, do it now...
    if (changes_required != False and "sizingcomment" in changes_required):
        comment = index_list[index_name].change_comment['sizingcomment']
        output_h.write(six.text_type(comment))
        logger.debug("Wrote the sizing comment=\"%s\"" % (comment[:-1]))