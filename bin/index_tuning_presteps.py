import logging

logger = logging.getLogger()


# Shared functions required by both index tuning/sizing & bucket tuning/sizing
def index_tuning_presteps(utility, index_list, index_ignore_list, earliest_license, latest_license, index_name_restriction, index_limit, indexerhostnamefilter, useIntrospectionData, indexes_not_getting_sized):
    logger.info("Running index_tuning_presteps")
    conf_files_to_check = {}
    for index in list(index_list.keys()):
        conf_file = index_list[index].conf_file
        # ignore known system files that we should not touch
        # TODO default to using a local file equivalent for non-system directories
        if conf_file.find("/etc/system/default/") == -1 and conf_file.find("_cluster/default/") == -1:
            conf_files_to_check[conf_file] = True

    logger.debug("conf_files_to_check=\"%s\"" % (conf_files_to_check))

    # parse all the conf files and look for comments about sizing (as this overrides settings later in the code)
    logger.info("Running parse_conf_files_for_sizing_comments()")
    # This just updates the index_list dictionary with new data
    utility.parse_conf_files_for_sizing_comments(index_list, conf_files_to_check)

    counter = 0
    index_count = len(index_list)

    if index_limit < index_count:
        index_count = index_limit

    for index_name in list(index_list.keys()):
        # If we have a restriction on which indexes to look at, skip the loop until we hit our specific index name
        if index_name_restriction:
            if index_name != index_name_restriction:
                continue

        # useful for manual runs
        logger.debug("iteration_count=%s of iteration_count=%s within loop" % (counter, index_count))

        # If we're performing a limited run quit the loop early when we reach the limit
        if counter > index_limit:
            break

        # Actually check license usage per index over the past X days, function returns three ints
        logger.info("index=%s running determine_license_usage_per_day" % (index_name))
        index_list[index_name].avg_license_usage_per_day, index_list[index_name].first_seen, index_list[index_name].max_license_usage_per_day = \
            utility.determine_license_usage_per_day(index_name, earliest_license, latest_license)

        # Determine compression ratio of each index, function returns floats, index_comp_ratio is re-used during index sizing so required by both index sizing
        # and bucket sizing scenarios
        logger.info("index=%s running determine_compression_ratio" % (index_name))
        index_list[index_name].index_comp_ratio, index_list[index_name].splunk_max_disk_usage_mb, index_list[index_name].oldest_data_found, index_list[index_name].newest_data_found = \
            utility.determine_compression_ratio(index_name, indexerhostnamefilter, useIntrospectionData)

        counter = counter + 1

    # At this point we have indexes that we are supposed to ignore in the dictionary, we need them there so we could
    # ensure that we didn't suggest deleting them from the filesystem, however now we can ignore them so we do not
    # attempt to re-size the indexes with no license info available
    logger.debug("The following indexes will be ignored as per configuration index_ignore_list=\"%s\"" % (index_ignore_list))
    for index in index_ignore_list:
        if index in index_list:
            indexes_not_getting_sized[index] = index_list[index]
            del index_list[index]
            logger.debug("Removing index=\"%s\" from index_list" % (index))

    #Metric indexes are excluded from tuning at this stage
    for index_name in list(index_list.keys()):
        datatype = index_list[index_name].datatype
        if datatype != 'event':
            logger.info("index=%s is excluded from tuning due to not been of type events, type=%s" % (index_name, datatype))
            indexes_not_getting_sized[index_name] = index_list[index_name]
            del index_list[index_name]