import logging
import datetime

logger = logging.getLogger()


#Functions exlusive to bucket sizing
def run_bucket_sizing(utility, index_list, index_name_restriction, index_limit, num_hours_per_bucket, bucket_contingency, upper_comp_ratio_level,
    min_size_to_calculate, num_of_indexers, rep_factor_multiplier):

    todays_date = datetime.datetime.now().strftime("%Y-%m-%d")

    counter = 0
    auto_high_volume_sizeMB = 10240
    index_count = len(index_list)

    if index_limit < index_count:
        index_count = index_limit

    logger.info("Running queries to determine bucket sizing, licensing, et cetera")
    # Actually run the various Splunk query functions to find bucket sizing, compression ratios and license usage
    for index_name in list(index_list.keys()):
        # If we have a restriction on which indexes to look at, skip the loop until we hit our specific index name
        if index_name_restriction:
            if index_name != index_name_restriction:
                continue
        # useful for manual runs
        logger.debug("%s iteration of %s within indexLoop" % (counter, index_count))

        # If we're performing a limited run quit the loop early when we reach the limit
        if counter > index_limit:
            break
        # function returns a float for recommended_bucket_size
        logger.info("index=%s, running determine_recommended_bucket_size" % (index_name))

        index_list[index_name].recommended_bucket_size = utility.determine_recommended_bucket_size(index_name, num_hours_per_bucket)
        # Add % bucket_contingency to bucket sizing
        index_list[index_name].recommended_bucket_size = index_list[index_name].recommended_bucket_size * bucket_contingency

        # If we have run the required checks on the index mark it True, otherwise do not, this is used later and relates to the limited index runs
        index_list[index_name].checked = True
        counter = counter + 1

    # Keep a dictionary of indexes requiring changes and conf files we need to output
    indexes_requiring_changes = {}
    conf_files_requiring_changes = []

    counter = 0
    logger.info("Now running bucket sizing calculations")
    for index_name in list(index_list.keys()):
        logger.debug("Working on index=%s with counter=%s" % (index_name, counter))
        # If we have a restriction on which indexes to look at, skip the loop until we hit our specific index name
        if index_name_restriction:
            if index_name != index_name_restriction:
                continue

        counter = counter + 1
        # If we're performing a limited run quit the loop early when we reach the limit
        if counter > index_limit:
            break

        # Shorter names to the various index attributes
        max_hot_buckets = index_list[index_name].max_hot_buckets
        bucket_size = index_list[index_name].max_data_size
        conf_file = index_list[index_name].conf_file
        recommended_bucket_size = index_list[index_name].recommended_bucket_size
        index_list[index_name].number_recommended_bucket_size = index_list[index_name].recommended_bucket_size

        if not hasattr(index_list[index_name], "index_comp_ratio"):
            logger.warn("index=%s has no data on disk so unable to do any bucket sizing calculations" % (index_name))
            continue
        index_comp_ratio = index_list[index_name].index_comp_ratio
        splunk_max_disk_usage_mb = index_list[index_name].splunk_max_disk_usage_mb
        oldest_data_found = index_list[index_name].oldest_data_found
        max_total_data_size_mb = float(index_list[index_name].max_total_data_size_mb)

        # If the compression ratio is unusually large warn but continue for now
        if index_comp_ratio > upper_comp_ratio_level:
            logger.info("index=%s, returned index_compression_ratio=%s, this is above the expected max_index_compression_ratio=%s, "\
            "this may break calculations changing this to index_compression_ratio=%s" % (index_name, index_comp_ratio, upper_comp_ratio_level, upper_comp_ratio_level))
            index_comp_ratio = upper_comp_ratio_level

        # If we have a really, really small amount of data such as hundreds of kilobytes the metadata can be larger than the raw data resulting in a compression ratio of 500
        # (i.e. the stored size is 500 times larger on disk than it is in raw data, resulting in other calculations such as bucket sizing getting broken
        # the alternative size calculation is used for this reason, and if the data is too small to calculate we use an upper bound on the ratio as a safety
        if splunk_max_disk_usage_mb > min_size_to_calculate:
            # If the data is poorly parsed (e.g. dates go well into the past) then the MB/day might be greater than what appears via dbinspect
            # and therefore we might need to sanity check this based on license usage * storage ratio / number of indexers / (potential hot buckets)
            # we add contingency to this as well
            alt_bucket_size_calc = ((index_list[index_name].max_license_usage_per_day * index_comp_ratio * rep_factor_multiplier) / num_of_indexers) / max_hot_buckets
            alt_bucket_size_calc = alt_bucket_size_calc * bucket_contingency

            if alt_bucket_size_calc > recommended_bucket_size:
                logger.info("index=%s alternative_bucket_size_calculation=%s, original recommended_bucket_size=%s, new recommended_bucket_size=%s" % (index_name, alt_bucket_size_calc, recommended_bucket_size, alt_bucket_size_calc))
                recommended_bucket_size = alt_bucket_size_calc
                index_list[index_name].recommended_bucket_size = recommended_bucket_size
                index_list[index_name].number_recommended_bucket_size = recommended_bucket_size
        else:
            logger.info("index=%s had a comp_ratio=%s and a splunk_total_size=%s, this is less than the lower_bound=%s, not performing the alternative bucket size calculation, oldest_data_found=%s days old" % (index_name, index_comp_ratio, splunk_max_disk_usage_mb, min_size_to_calculate, oldest_data_found))
        # We only change values where required, otherwise we output the line as we read it
        requires_change = False
        # If we didn't auto tune the bucket and it's a lot smaller or bigger than we change the values to the new numbers
        if bucket_size.find("auto") == -1:
            logger.warn("Not an auto sized bucket for index=" + index_name + " this index will be excluded from sizing")
            continue
        # It is an auto sized bucket, this makes it slightly different
        logger.debug("index=%s auto sized bucket with bucket_size=%s" % (index_name, bucket_size))
        end = bucket_size.find("_")
        # With auto sized buckets we probably care more when the buckets are too small rather than too large (for now)
        bucketAutoSize = float(bucket_size[0:end])
        percDiff = (100 / bucketAutoSize)*recommended_bucket_size

        # If we expect to exceed the auto size in use, go to the auto_high_volume setting, assuming we are not already there
        if percDiff > 100 and not bucket_size == "10240_auto":
            homepath_max_data_size_mb = index_list[index_name].homepath_max_data_size_mb

            logger.debug("homepath_max_data_size_mb=%s and auto_high_volume_sizeMB * max_hot_buckets calc=%s and max_total_data_size_mb=%s"
                % (homepath_max_data_size_mb, auto_high_volume_sizeMB * max_hot_buckets, max_total_data_size_mb))
            if homepath_max_data_size_mb != 0.0 and (auto_high_volume_sizeMB * max_hot_buckets) > homepath_max_data_size_mb:
                logger.warn("index=%s would require an auto_high_volume (10GB) bucket but the homepath_max_data_size_mb=%s "\
                            "cannot fit max_hot_buckets=%s of that size, not changing the bucket sizing" % (index_name, homepath_max_data_size_mb, max_hot_buckets))
            elif homepath_max_data_size_mb == 0.0 and (auto_high_volume_sizeMB * max_hot_buckets) > max_total_data_size_mb:
                logger.warn("index=%s would require an auto_high_volume (10GB) bucket but the max_total_data_size_mb=%s "\
                            "cannot fit max_hot_buckets=%s buckets of that size, not changing the bucket sizing" % (index_name, max_total_data_size_mb, max_hot_buckets))
            else:
                requires_change = "bucket"
                # If we don't have any change comments so far create the dictionary
                if not hasattr(index_list[index_name], "change_comment"):
                    index_list[index_name].change_comment = {}
                # Write comments into the output files so we know what tuning occured and when
                index_list[index_name].change_comment['bucket'] = "# Bucket size increase required estimated %s, auto-tuned on %s\n" % (index_list[index_name].number_recommended_bucket_size, todays_date)
                # Simplify to auto_high_volume
                index_list[index_name].recommended_bucket_size = "auto_high_volume"
                logger.info("index=%s file=%s current bucket size is auto tuned maxDataSize=%s, recommended_bucket_size=%s "\
                "(will be set to auto_high_volume (size increase)), max_hot_buckets=%s" % (index_name, conf_file, bucket_size, recommended_bucket_size, max_hot_buckets))
        else:
            # Bucket is smaller than current sizing, is it below the auto 750MB default or not, and is it currently set to a larger value?
            if recommended_bucket_size < 750 and bucketAutoSize > 750:
                requires_change = "bucket"
                # If we don't have any change comments so far create the dictionary
                if not hasattr(index_list[index_name],"change_comment"):
                    index_list[index_name].change_comment = {}

                # Write comments into the output files so we know what tuning occured and when
                index_list[index_name].change_comment['bucket'] = "# Bucket size decrease required estimated %s, auto-tuned on %s\n" % (index_list[index_name].number_recommended_bucket_size, todays_date)
                index_list[index_name].recommended_bucket_size = "auto"
                logger.info("index=%s file=%s current bucket size is auto tuned to maxDataSize=%s, recommended_bucket_size=%s "\
                    "(will be set to auto (size decrease)), max_hot_buckets=%s" % (index_name, conf_file, bucket_size, recommended_bucket_size, max_hot_buckets))

        # If this index requires change we record this for later
        if requires_change != False:
            indexes_requiring_changes[index_name] = requires_change
            logger.debug("index=%s requires changes of change_type=%s" % (index_name, requires_change))

            # Add the conf file to the list we need to work on
            if conf_file not in conf_files_requiring_changes:
                conf_files_requiring_changes.append(conf_file)
                logger.debug("index=%s resulted in file=%s added to change list" % (index_name, conf_file))

    return indexes_requiring_changes, conf_files_requiring_changes