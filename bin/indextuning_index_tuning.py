import logging
import datetime

logger = logging.getLogger()


def run_index_sizing(utility, index_list, index_name_restriction, index_limit, num_of_indexers, lower_index_size_limit, sizing_continency, min_days_of_license_for_sizing, perc_before_adjustment,
    do_not_lose_data_flag, undersizing_continency, smallbucket_size, skip_problem_indexes_flag, indexes_requiring_changes, conf_files_requiring_changes, rep_factor_multiplier, upper_comp_ratio_level,
    no_sizing_comments):

    todays_date = datetime.datetime.now().strftime("%Y-%m-%d")

    counter = 0
    index_count = len(index_list)

    if index_limit < index_count:
        index_count = index_limit

    calculated_size_total = 0

    counter = 0
    logger.info("Running queries to determine index sizing")
    for index_name in list(index_list.keys()):
        logger.debug("index=%s counter=%s counter_limit=%s" % (index_name, counter, index_limit))
        # If we have a restriction on which indexes to look at, skip the loop until we hit our specific index name
        if index_name_restriction:
            if index_name != index_name_restriction:
                continue

        counter = counter + 1
        # If we're performing a limited run quit the loop early when we reach the limit
        if counter > index_limit:
            break

        conf_file = index_list[index_name].conf_file
        if not hasattr(index_list[index_name], 'index_comp_ratio'):
            logger.warn("index=%s has no data on disk, not doing any sizing" % (index_name))
            continue

        #Creating variables to make these easier to access later in the code
        index_comp_ratio = index_list[index_name].index_comp_ratio
        splunk_max_disk_usage_mb = float(index_list[index_name].splunk_max_disk_usage_mb)
        oldest_data_found = index_list[index_name].oldest_data_found

        frozen_time_period_in_days = int(index_list[index_name].frozen_time_period_in_secs)/60/60/24
        max_total_data_size_mb = float(index_list[index_name].max_total_data_size_mb)
        avg_license_usage_per_day = index_list[index_name].avg_license_usage_per_day
        license_data_first_seen = index_list[index_name].first_seen

        # If the compression ratio is unusually large warn but continue for now
        if index_comp_ratio > upper_comp_ratio_level:
            logger.info("index=%s, returned index_compression_ratio=%s, this is above the expected max_index_compression_ratio=%s, this may break calculations in the script " \
                "changing this to index_compression_ratio=%s" % (index_name, index_comp_ratio, upper_comp_ratio_level, upper_comp_ratio_level))
            index_comp_ratio = upper_comp_ratio_level

        summary_index = False
        # Zero license usage, this could be a summary index
        if avg_license_usage_per_day == 0:
            json_result = utility.run_search_query("| metadata index=%s type=sourcetypes | table sourcetype" % (index_name))

            if len(json_result["results"]) == 1 and json_result["results"][0]["sourcetype"] == "stash":
                # At this point we know its a summary index
                # So we can use the average growth rate to determine if any sizing changes are required
                json_result = utility.run_search_query(""" search index=_introspection \"data.name\"=\"%s\"
                | bin _time span=1d
                | stats max(data.total_size) AS total_size by host, _time
                | streamstats current=f window=1 max(total_size) AS prev_total by host
                | eval diff=total_size - prev_total
                | stats avg(diff) AS avgchange by host
                | stats avg(avgchange) AS overallavg""" % (index_name))

                if len(json_result["results"]) == 1:
                    summary_usage_change_per_day = float(json_result["results"][0]["overallavg"])
                    logger.info("index=%s is a summary index, average_change_per_day=%s from introspection logs" % (index_name, summary_usage_change_per_day))
                else:
                    logger.info("index=%s is a summary index, could not find average_change_per_day from introspection logs average_change_per_day=0.0" % (index_name))
                    summary_usage_change_per_day = 0.0
                summary_index = True

        # Company specific field here, the commented size per day in the indexes.conf file
        sizing_comment = -1
        if hasattr(index_list[index_name],"size_per_day_in_mb"):
            sizing_comment = int(index_list[index_name].size_per_day_in_mb)

        oversized = False
        estimated_days_for_current_size = 0
        # calculate the index size usage based on recent license usage data and divide by the number of indexers we have
        calculated_size = (index_comp_ratio * avg_license_usage_per_day * frozen_time_period_in_days * rep_factor_multiplier)/num_of_indexers

        # This index has data but no incoming data during the measurement period (via the license logs), capping the index at current size + contingency
        # or the estimated size if we have it
        if calculated_size == 0.0:
            # We have zero incoming data so we should be fine to store the frozen time period in days amount of data
            estimated_days_for_current_size = frozen_time_period_in_days

            # If this indexed was never sized through the sizing script *and* it has no recent data *and* it is not a summary index
            # then we cap it at current size + contingency rather than just leaving it on defaults
            # If it had a configured size it's dealt with later in the code
            if sizing_comment < 0 and not summary_index:
                # ensure that our new sizing does not drop below what we already have on disk
                largest_on_disk_size = splunk_max_disk_usage_mb
                 #add the contingency calculation in as we don't want to size to the point where we drop data once we apply on any of the indexers
                largest_on_disk_size = int(round(largest_on_disk_size * sizing_continency))
                # This now becomes our tuned size
                # However we cannot go below the lower index size limit restriction...

                if largest_on_disk_size < lower_index_size_limit:
                    largest_on_disk_size = lower_index_size_limit

                logger.info("index=%s has zero incoming data for time period, capping size per indexer at size=%s" % (index_name, largest_on_disk_size))

                # Store the calculated value for later use
                index_list[index_name].calc_max_total_data_size_mb = largest_on_disk_size
        # The index had recent data so we can estimate approx number of days we have left
        # calculated_size is the amount of data we will use per indexer
        else:
            if index_comp_ratio == 0.0:
                estimated_days_for_current_size = frozen_time_period_in_days
            else:
                if summary_index:
                    estimated_days_for_current_size = int(round(max_total_data_size_mb / (calculated_size * sizing_continency)))
                else:
                    estimated_days_for_current_size = int(round(max_total_data_size_mb / ((index_comp_ratio * avg_license_usage_per_day * sizing_continency * rep_factor_multiplier)/num_of_indexers)))

        index_list[index_name].estimated_total_data_size = int(calculated_size)
        # We leave a bit of room spare just in case by adding a contingency sizing here
        calculated_size = int(round(calculated_size*sizing_continency))
        index_list[index_name].estimated_total_data_size_with_contingency = calculated_size

        if summary_index:
            largest_on_disk_size = splunk_max_disk_usage_mb
            if summary_usage_change_per_day < 0.0:
                calculated_size = int(largest_on_disk_size * sizing_continency)

                # Store the estimate of how much we will likely use based on sizing calculations ignoring contingency
                index_list[index_name].estimated_total_data_size = int(largest_on_disk_size)
                index_list[index_name].estimated_total_data_size_with_contingency = calculated_size
                logger.info("index=%s appears to have summary_usage_change_per_day=%s (zero or less), calculated_size=%s as the size that this index will need (includes contingency=%s)"
                            % (index_name, summary_usage_change_per_day, calculated_size, sizing_continency))
            else:
                # Calculate the size per indexer as approx currentSize * contingency value * change per day * amount of time the data is kept in summary
                calculated_size = int(largest_on_disk_size + (sizing_continency * summary_usage_change_per_day * frozen_time_period_in_days))
                # Store the estimate of how much we will likely use based on sizing calculations ignoring contingency
                index_list[index_name].estimated_total_data_size = int(largest_on_disk_size + (summary_usage_change_per_day * frozen_time_period_in_days))
                index_list[index_name].estimated_total_data_size_with_contingency = calculated_size

                logger.info("index=%s summary_usage_change_per_day=%s calculated_size=%s as the size that this summary index will need (includes contingency=%s)"
                            % (index_name, summary_usage_change_per_day, calculated_size, sizing_continency))

        max_data_size = index_list[index_name].max_data_size
        if max_data_size == "10240_auto":
            max_data_size = 10240
        elif max_data_size == "750_auto":
            max_data_size = 750
        else:
            max_data_size = int(max_data_size)

        # If we are within 2 buckets worth of the max data size assume the index is 100% full
        if (splunk_max_disk_usage_mb + (2*max_data_size)) > max_total_data_size_mb:
            index_list[index_name].perc_utilised = 100
            index_list[index_name].perc_utilised_on_estimate = 100
            index_list[index_name].days_until_full = 0
            index_list[index_name].days_until_full_disk_calculation = 0
            index_list[index_name].days_until_full_disk_calculation_on_estimate = 0
            logger.debug("index=%s perc_utilised=100 days_until_full=0 because splunk_max_disk_usage_mb=%s + 2*%s > %s, perc_utilised_on_estimate=100, days_until_full_disk_calculation_on_estimate=0"
                        % (index_name, splunk_max_disk_usage_mb, max_data_size, max_total_data_size_mb))
        else:
            # Estimate the perc full we are
            index_list[index_name].perc_utilised = round((splunk_max_disk_usage_mb / max_total_data_size_mb)*100)
            available_index_mb = max_total_data_size_mb - splunk_max_disk_usage_mb
            if index_comp_ratio * avg_license_usage_per_day * rep_factor_multiplier == 0.0:
                days_until_full_disk_calculation = frozen_time_period_in_days
            else:
                days_until_full_disk_calculation = int(round(available_index_mb / ((index_comp_ratio * avg_license_usage_per_day * rep_factor_multiplier)/num_of_indexers)))

            if days_until_full_disk_calculation > frozen_time_period_in_days:
                days_until_full_disk_calculation = frozen_time_period_in_days
            else:
                days_until_full_disk_calculation = days_until_full_disk_calculation

            days_until_full = frozen_time_period_in_days - int(index_list[index_name].oldest_data_found)
            if days_until_full < 0:
                logger.warn("index=%s days_until_full=%s seems inaccurate calculated from oldest_data_found=%s and frozen_time_period_in_days=%s changing this to zero to assume this is full"
                    % (index_name, days_until_full, oldest_data_found, frozen_time_period_in_days))
                days_until_full = 0

            if splunk_max_disk_usage_mb == 0.0:
                perc_utilised_on_estimate = 0
                days_until_full_disk_calculation_on_estimate = frozen_time_period_in_days
                perc_utilised_on_estimate = 0
            else:
                perc_utilised_on_estimate = index_list[index_name].estimated_total_data_size  / splunk_max_disk_usage_mb
                days_until_full_disk_calculation_on_estimate = frozen_time_period_in_days - int(round(perc_utilised_on_estimate * frozen_time_period_in_days))
                perc_utilised_on_estimate = int(round(perc_utilised_on_estimate * 100))

            logger.debug("index=%s perc_utilised=%s days_until_full=%s days_until_full_disk_calculation=%s calculated using index_comp_ratio=%s, " \
                "avg_license_usage_per_day=%s, rep_factor_multiplier=%s, num_of_indexers=%s, perc_utilised_on_estimate=%s, days_until_full_disk_calculation_on_estimate=%s"
                % (index_name, index_list[index_name].perc_utilised, days_until_full,days_until_full_disk_calculation, index_comp_ratio, avg_license_usage_per_day,
                rep_factor_multiplier, num_of_indexers, perc_utilised_on_estimate, days_until_full_disk_calculation_on_estimate))

            index_list[index_name].days_until_full = days_until_full
            index_list[index_name].days_until_full_disk_calculation = days_until_full_disk_calculation
            index_list[index_name].perc_utilised_on_estimate = perc_utilised_on_estimate
            index_list[index_name].days_until_full_disk_calculation_on_estimate = days_until_full_disk_calculation_on_estimate

        min_size_override = False
        # Bucket explosion occurs if we undersize an index too much so cap at the lower size limit
        if calculated_size < lower_index_size_limit:
            calculated_size = lower_index_size_limit

        min_req_size = int(index_list[index_name].max_hot_buckets) * max_data_size
        if calculated_size < min_req_size:
            logger.warn("index=%s, calc_max_total_data_size_mb=%s less than min_req_size=%s (based on bucket_size=%s*max_hot_buckets=%s), "\
                " frozen_time_period_in_days=%s, max_total_data_size_mb=%s , "\
                "avg_license_usage_per_day=%s, sizing_comment=%s, index_comp_ratio=%s, calculated_size=%s, estimated_days_for_current_size=%s, "\
                "oldest_data_found=%s, rep_factor_multiplier=%s, changing size back to %s"
                % (index_name, calculated_size, min_req_size, max_data_size, index_list[index_name].max_hot_buckets,
                   frozen_time_period_in_days, max_total_data_size_mb, avg_license_usage_per_day, sizing_comment, index_comp_ratio, calculated_size,
                   estimated_days_for_current_size, oldest_data_found, rep_factor_multiplier, min_req_size))

            calculated_size = min_req_size
            min_size_override = True

        # Add our calculated size back in for later, int just in case the lower_index_size_limit is a float
        index_list[index_name].calc_max_total_data_size_mb = int(round(calculated_size))

        # This flag is set if the index is undersized on purpose (i.e. we have a setting that says to set it below the limit where we lose data
        do_not_increase = False

        # This index was never sized so auto-size it
        if sizing_comment < 0:
            if not calculated_size:
                # Assume we're ok as we have no data to say otherwise...this could be for example a new index about to receive data
                # so do nothing here
                oversized = False
            elif license_data_first_seen < min_days_of_license_for_sizing:
                oversized = False
            elif max_total_data_size_mb == 0:
                logger.warn("index=%s, max_total_data_size_mb==0, invalid setting" % (index_name))
            else:
                # If we have oversized the index by a significant margin we do something, if not we take no action
                perc_est = calculated_size / max_total_data_size_mb
                logger.debug("perc_est=%s for calculated_size/maxTotalSizeMB" % (perc_est))
                if perc_est < perc_before_adjustment:
                    # Index is oversized > ?% therefore we can use our new sizing to deal with this
                    logger.info("index=%s is oversized, perc_est=%s or calculatedSize/maxTotalSize (%s/%s) is less than the perc_before_adjustment=%s, "\
                        "index will not be resized, oldest_data_found=%s" % (index_name, perc_est, calculated_size,  max_total_data_size_mb, perc_before_adjustment, oldest_data_found))
                    oversized = True
        # We have a configured size of the indexer
        else:
            if not calculated_size:
                # Assume we're ok as we have no data to say otherwise...this could be for example a new index about to receive data
                # so do nothing here
                oversized = False
            # In this scenario we have some license data but not enough to determine the index is oversized, so assume false as this isn't a risk...
            elif license_data_first_seen < min_days_of_license_for_sizing:
                logger.debug("index=%s not enough days of license data to do sizing, we have license_data_first_seen=%s" % (index_name, license_data_first_seen))
                oversized = False
            else:
                # size estimate on disk based on the compression ratio we have seen, and the the configured size in the config file
                size_estimate = (sizing_comment * index_comp_ratio * frozen_time_period_in_days * rep_factor_multiplier)/num_of_indexers
                # including contingency
                size_estimate = size_estimate * sizing_continency

                # 3000MB is the lower bound as 3*auto sized buckets + a little extra is 3000MB
                if size_estimate < lower_index_size_limit:
                    size_estimate = lower_index_size_limit

                usage_based_caculated_size = index_list[index_name].calc_max_total_data_size_mb
                # If the sizing was previously decided during a sizing discussion, then allocate the requested size
                index_list[index_name].calc_max_total_data_size_mb = int(round(size_estimate))
                calc_size_per_day_based_on_commented_size = index_list[index_name].calc_max_total_data_size_mb
                logger.debug("index=%s, based on previous calculations the max_total_data_size_mb=%sMB, however sizing_comment=%sMB/day "\
                    "so re-calculated max_total_data_size_mb=%sMB, oldest_data_found=%s days old"
                    % (index_name, usage_based_caculated_size, sizing_comment, index_list[index_name].calc_max_total_data_size_mb, oldest_data_found))

                # Skip the zero size estimated where index_comp_ratio == 0.0 or license usage is zero
                if index_comp_ratio != 0.0 and avg_license_usage_per_day != 0:
                    estimated_days_for_current_size = int(round(max_total_data_size_mb / ((index_comp_ratio * avg_license_usage_per_day * sizing_continency * rep_factor_multiplier)/num_of_indexers)))

                # If the commented size would result in data loss or an undersized index and we have a comment about this it's ok to keep it undersized
                if usage_based_caculated_size > calc_size_per_day_based_on_commented_size:
                    if do_not_lose_data_flag or index_list[index_name].datatype == "metric":
                        index_list[index_name].calc_max_total_data_size_mb = usage_based_caculated_size
                        logger.info("index=%s has more data than expected and has sizing_comment=%s however avg_license_usage_per_day=%s, "\
                            "current size would fit days=%s, frozenTimeInDays=%s, increasing the size of this index, oldest data found is days=%s old, "\
                            "rep_factor_multiplier=%s, datatype=%s, usage_based_caculated_size=%s, calc_size_per_day_based_on_commented_size=%s"
                            % (index_name, sizing_comment, avg_license_usage_per_day, estimated_days_for_current_size, 
                            frozen_time_period_in_days, oldest_data_found, rep_factor_multiplier, index_list[index_name].datatype,
                            usage_based_caculated_size, calc_size_per_day_based_on_commented_size))
                    elif min_size_override:
                        index_list[index_name].calc_max_total_data_size_mb = usage_based_caculated_size
                    else:
                        logger.warn("index=%s has more data than expected and has sizing_comment=%s however avg_license_usage_per_day=%s, data loss is likely after days=%s, "\
                            "frozenTimeInDays=%s, oldest data found is days=%s old, rep_factor_multiplier=%s, making no changes here as do_not_lose_data_flag is false and this index has "\
                            "a sizing_comment, usage_based_caculated_size=%s, calc_size_per_day_based_on_commented_size=%s"
                            % (index_name, sizing_comment, avg_license_usage_per_day, estimated_days_for_current_size, frozen_time_period_in_days, oldest_data_found, rep_factor_multiplier,
                              usage_based_caculated_size, calc_size_per_day_based_on_commented_size))

                # If the newly calculated size has increased compared to previous size multiplied by the undersizing contingency
                elif estimated_days_for_current_size < frozen_time_period_in_days:
                    # We will increase the sizing for this index
                    logger.info("index=%s requires more sizing max_total_data_size_mb=%s sizing_comment=%s, sizing_comment=%s, frozen_time_period_in_days=%s, avg_license_usage_per_day=%s, "\
                         "oldest data found is days=%s old, rep_factor_multiplier=%s"
                         % (index_name, max_total_data_size_mb, calc_size_per_day_based_on_commented_size, sizing_comment, frozen_time_period_in_days,
                         avg_license_usage_per_day, oldest_data_found, rep_factor_multiplier))

                # At some point this index was manually sized to be bigger than the comment, fix it now, this may drop it below the expected frozen time period in days
                if max_total_data_size_mb > calc_size_per_day_based_on_commented_size:
                    # Determine the % difference between the new estimate and the previous max_total_data_size_mb= setting
                    perc_est = calc_size_per_day_based_on_commented_size / max_total_data_size_mb
                    logger.debug("perc_est=%s based on %s %s %s %s and max total %s" % (perc_est, sizing_comment, index_comp_ratio, frozen_time_period_in_days, num_of_indexers, max_total_data_size_mb))
                    # If we are below the threshold where we adjust take action
                    if perc_est < perc_before_adjustment:
                        oversized = True
                        do_not_increase = True

                        # We warn if we drop below the frozen time period in seconds and do not warn if we are staying above it
                        str = "index=%s sizing_comment=%s index_comp_ratio=%s max_total_data_size_mb=%s calc_size_per_day_based_on_commented_size=%s frozen_time_period_in_days=%s "\
                            "estimated_days_for_current_size=%s, avg_license_usage_per_day=%s, this index will be decreased in size, oldest_data_found=%s days old rep_factor_multiplier=%s" \
                                % (index_name, sizing_comment, index_comp_ratio, max_total_data_size_mb, calc_size_per_day_based_on_commented_size, frozen_time_period_in_days,
                                estimated_days_for_current_size, avg_license_usage_per_day, oldest_data_found, rep_factor_multiplier)
                        if estimated_days_for_current_size < frozen_time_period_in_days:
                            logger.warn(str + " risk of data loss")
                        else:
                            logger.info(str)

        requires_change = False

        # Deal with the fact that bucket sizing changes may be occurring to these indexes
        # therefore set the requires_change status if it exists...
        if index_name in indexes_requiring_changes:
            requires_change = indexes_requiring_changes[index_name]
            logger.debug("index=%s requires_change=%s" % (index_name, requires_change))

        # If the estimates show we cannot store the expected frozen_time_period_in_days on disk we need to take action
        # this is an undersized scenario and we need a larger size unless of course we have already capped the index at this size and data loss is expected
        if (estimated_days_for_current_size < frozen_time_period_in_days and not do_not_increase) or min_size_override:
            if min_size_override:
                logger.info("index=%s requires an increase due to been below minimum sizing requirements" % (index_name))
            else:                
                logger.info("index=%s has less storage than the frozen time period in days, estimated_days_for_current_size=%s frozen_time_period_in_days=%s an increase may be required"
                            % (index_name, estimated_days_for_current_size, frozen_time_period_in_days))
                # TODO if sizing_comment != "N/A" do we still increase an undersized index or let it get frozen anyway because the disk estimates were invalid?!
                # also the above would be assuming that the disk estimates were based on the indexers we have now configured
                # for now assuming we always want to increase and prevent data loss...

            if max_total_data_size_mb == index_list[index_name].calc_max_total_data_size_mb:
                logger.info("index=%s max_total_data_size_mb=%s is the new calculated size, therefore no changes required here" % (index_name, max_total_data_size_mb))
            # If we appear to be undersized but we don't have enough data yet
            elif license_data_first_seen < min_days_of_license_for_sizing:
                logger.info("index=%s appears to be undersized but license_data_first_seen=%s days ago, min_days_of_license_for_sizing=%s, frozen_time_period_in_days=%s max_total_data_size_mb=%s, "\
                    "avg_license_usage_per_day=%s, sizing_comment=%s, index_comp_ratio=%s, calculated_size=%s, "\
                    "estimated_days_for_current_size=%s, oldest_data_found=%s days ago"
                    % (index_name, license_data_first_seen, min_days_of_license_for_sizing, frozen_time_period_in_days, max_total_data_size_mb,
                    avg_license_usage_per_day, sizing_comment, index_comp_ratio, calculated_size, estimated_days_for_current_size, oldest_data_found))
            # If we have enough data and we're doing a bucket size adjustment we are now also doing the index sizing adjustment
            elif requires_change != False and oversized != True:
                adjust_if_above = max_total_data_size_mb * undersizing_continency
                if index_list[index_name].calc_max_total_data_size_mb > adjust_if_above:
                    logger.debug("index=%s calc_max_total_data_size_mb=%s which is greater than adjust_if_above=%s based on max_total_data_size_mb*undersizing_continency (%s*%s)"
                    % (index_name, index_list[index_name].calc_max_total_data_size_mb, adjust_if_above, max_total_data_size_mb, undersizing_continency))
                    requires_change = requires_change + "_sizing"
                    # Write comments into the output files so we know what tuning occured and when
                    index_list[index_name].change_comment['sizing'] = "# max_total_data_size_mb previously %s, auto-tuned on %s\n" % (max_total_data_size_mb, todays_date)
                else:
                    logger.info("index=%s, (bucket & index sizing), index is undersized however an adjustment is only going to occur once the "\
                    "newly calculated size is greater than adjust_if_above=%s, currently it is calc_max_total_data_size_mb=%s" % (index_name, adjust_if_above, index_list[index_name].calc_max_total_data_size_mb))
            # We need to do an index sizing adjustment
            else:
                # If we previously said the index was oversized, but it is undersized, this can only happen when the comment advising the size is exceeded by the size of the data
                # depending on policy we can either not re-size this *or* we can just re-size it to fix the data we received, for now using re-sizing based on what we have recieved
                # rather than the original estimate
                if oversized == True:
                    logger.info("index=%s undersized and has license_data_first_seen=%s days of license data *but* index sizing comment advises index oversized, increasing size, "\
                    "min_days_of_license_for_sizing=%s, frozen_time_period_in_days=%s, max_total_data_size_mb=%s avg_license_usage_per_day=%s, sizing_comment=%s, "\
                    "index_comp_ratio=%s, calculated_size=%s, estimated_days_for_current_size=%s, oldest_data_found=%s, rep_factor_multiplier=%s"
                    % (index_name, license_data_first_seen, min_days_of_license_for_sizing, frozen_time_period_in_days, max_total_data_size_mb, avg_license_usage_per_day,
                    sizing_comment, index_comp_ratio, calculated_size, estimated_days_for_current_size, oldest_data_found, rep_factor_multiplier))

                    oversized = False

                adjust_if_above = max_total_data_size_mb * undersizing_continency
                if index_list[index_name].calc_max_total_data_size_mb > adjust_if_above:
                    logger.debug("index=%s adjust_if_above=%s calc_max_total_data_size_mb=%s" % (index_name, adjust_if_above, index_list[index_name].calc_max_total_data_size_mb))
                    requires_change = "sizing"
                    if not hasattr(index_list[index_name],'change_comment'):
                        index_list[index_name].change_comment = {}

                    # Write comments into the output files so we know what tuning occured and when
                    str = "# max_total_data_size_mb previously %s, had room for %s days, auto-tuned on %s\n" % (index_list[index_name].max_total_data_size_mb, estimated_days_for_current_size, todays_date)
                    index_list[index_name].change_comment['sizing'] = str
                else:
                    logger.info("index=%s (index sizing only) is undersized however an adjustment is only going to occur once the newly "\
                        "calculated size is adjust_if_above=%s, currently it is calc_max_total_data_size_mb=%s" % (index_name, adjust_if_above, index_list[index_name].calc_max_total_data_size_mb))
            # Record this in our tuning log
            logger.info("index=%s undersized, frozen_time_period_in_days=%s, max_total_data_size_mb=%s, avg_license_usage_per_day=%s, sizing_comment=%s, "\
                        "index_comp_ratio=%s, calculated_size=%s, estimated_days_for_current_size=%s, license_data_first_seen=%s days ago, " \
                        "oldest_data_found=%s days ago, rep_factor_multiplier=%s"
                         % (index_name, frozen_time_period_in_days, max_total_data_size_mb, avg_license_usage_per_day, sizing_comment, index_comp_ratio,
                         calculated_size, estimated_days_for_current_size, license_data_first_seen, oldest_data_found, rep_factor_multiplier))

        # Is this index oversized in our settings
        if oversized == True:
            # ensure that our new sizing does not drop below what we already have on disk
            # as this would cause data to freeze on at least 1 indexer
            largest_on_disk_size = float(index_list[index_name].splunk_max_disk_usage_mb)
            largest_on_disk_size = int(round(largest_on_disk_size * sizing_continency))

            maxTotalDataSize = index_list[index_name].max_total_data_size_mb

            str = "# max_total_data_size_mb previously %s, auto-tuned on %s\n" % (maxTotalDataSize, todays_date)

            calc_max_total_data_size_mb = index_list[index_name].calc_max_total_data_size_mb

            # Sanity check to ensure our tuned size doesn't drop below our largest on-disk size
            if calc_max_total_data_size_mb < largest_on_disk_size:
                logger.warn("index=%s oversized, calc_max_total_data_size_mb=%s less than largest_on_disk_size=%s, frozen_time_period_in_days=%s, max_total_data_size_mb=%s , "\
                    "avg_license_usage_per_day=%s, sizing_comment=%s, index_comp_ratio=%s, calculated_size=%s, estimated_days_for_current_size=%s, "\
                    "oldest_data_found=%s, rep_factor_multiplier=%s, refusing to trigger immediate data loss and changing back to largest_on_disk_size (%s)"
                    % (index_name, calc_max_total_data_size_mb, largest_on_disk_size, frozen_time_period_in_days, max_total_data_size_mb, avg_license_usage_per_day,
                    sizing_comment, index_comp_ratio, calculated_size, estimated_days_for_current_size, oldest_data_found, rep_factor_multiplier, largest_on_disk_size))

                index_list[index_name].calc_max_total_data_size_mb = largest_on_disk_size
                index_list[index_name].estimated_total_data_size = int(index_list[index_name].splunk_max_disk_usage_mb)
                index_list[index_name].estimated_total_data_size_with_contingency = largest_on_disk_size

            # Write comments into the output files so we know what tuning occured and when
            if not hasattr(index_list[index_name], 'change_comment'):
                index_list[index_name].change_comment = {}

            # If we have skip problem indexes on *and* we are reducing an index in size that would result in a cap at current disk usage levels
            # then we skip it as it might lose data after this change
            if skip_problem_indexes_flag and calc_max_total_data_size_mb < largest_on_disk_size:
                logger.info("index=%s oversized but skip_problem_indexes_flag is on and calc_max_total_data_size_mb=%s < largest_on_disk_size=%s, therefore sparing this index for sizing based adjustments"
                % (index_name, calc_max_total_data_size_mb, largest_on_disk_size))
            else:
                if calc_max_total_data_size_mb < largest_on_disk_size:
                    logger.warn("index=%s skip_problem_indexes_flag is not set and calc_max_total_data_size_mb=%s < largest_on_disk_size=%s, this will result in data loss (FYI only)"
                    % (index_name, calc_max_total_data_size_mb, largest_on_disk_size))

                index_list[index_name].change_comment['sizing'] = str
                # If we are already changing the bucket sizing we are now also changing the index sizing
                if requires_change != False:
                    requires_change = requires_change + "_sizing"
                # We are changing the index sizing only
                else:
                    requires_change = "sizing"

            #Record this in our log
            logger.info("index=%s oversized, frozen_time_period_in_days=%s, max_total_data_size_mb=%s, avg_license_usage_per_day=%s, sizing_comment=%s, index_comp_ratio=%s, "\
                "calculated_size=%s, estimated_days_for_current_size=%s, license_data_first_seen=%s days ago, oldest_data_found=%s days ago, rep_factor_multiplier=%s"
                % (index_name, frozen_time_period_in_days, max_total_data_size_mb, avg_license_usage_per_day, sizing_comment, index_comp_ratio, calculated_size,
                estimated_days_for_current_size, license_data_first_seen, oldest_data_found, rep_factor_multiplier))

        # If we haven't yet added a sizing comment and we need one, this only happens during the initial runs
        # after this all indexes should have comments and this shouldn't run
        if sizing_comment < 0 and not no_sizing_comments:
            if requires_change != False:
                requires_change = requires_change + "_sizingcomment"
            else:
                requires_change = "sizingcomment"

            if not hasattr(index_list[index_name], 'change_comment'):
                index_list[index_name].change_comment = {}

            # Write comments into the output files so we know what tuning occured and when
            str = "# auto-size comment created on %s (%s days @ %sMB/day @ %s compression ratio)\n" % (todays_date, frozen_time_period_in_days, avg_license_usage_per_day, index_comp_ratio)

            index_list[index_name].change_comment['sizingcomment'] = str
            logger.warn("index=%s adding sizing comment of comment=\"%s\" as was unable to find a sizing comment of any kind" % (index_name, str))

        # If this index requires change we record this for later
        if requires_change != False:
            indexes_requiring_changes[index_name] = requires_change
            logger.debug("index=%s requires_change=%s" % (index_name, requires_change))

            # An additional scenario occurs that we have calculated the max_total_data_size_mb but we have also custom set the coldPath.max_data_sizeMB
            # and homePath.max_data_sizeMB and therefore must calculate them now...(as they will be invalid after we re-size the index)
            homepath_max_data_size_mb = index_list[index_name].homepath_max_data_size_mb
            cur_homepath_max_data_size_mb = index_list[index_name].homepath_max_data_size_mb
            cur_cold_path_max_data_size_mb = index_list[index_name].coldpath_max_datasize_mb
            cold_path_max_data_size_mb = cur_cold_path_max_data_size_mb

            skipconf_file = False
            # if we're only changing the sizing comment then don't attempt sizing the max_data_sizeMB
            if requires_change == "sizingcomment":
                pass
            # We custom specified our homePathDataSize, therefore assume we need to calculate both values here
            elif homepath_max_data_size_mb != 0.0:
                perc_multiplier = homepath_max_data_size_mb / max_total_data_size_mb

                # Assume that this % is the amount of data we should allocate to the homePath.max_data_sizeMB
                calc_max_total_data_size_mb = index_list[index_name].calc_max_total_data_size_mb
                homepath_max_data_size_mb = int(round(calc_max_total_data_size_mb * perc_multiplier))
                cold_path_max_data_size_mb = calc_max_total_data_size_mb - homepath_max_data_size_mb

                if homepath_max_data_size_mb < lower_index_size_limit:
                    size_added = lower_index_size_limit - homepath_max_data_size_mb
                    cold_path_max_data_size_mb = cold_path_max_data_size_mb - size_added
                    homepath_max_data_size_mb = lower_index_size_limit

                    # We don't want this number too small either, at least 1 bucket should fit here
                    if cold_path_max_data_size_mb < smallbucket_size:
                        cold_path_max_data_size_mb = smallbucket_size

                    index_list[index_name].calc_max_total_data_size_mb = homepath_max_data_size_mb + cold_path_max_data_size_mb

                # Add the newly calculated numbers back in for later use in the indexes.conf output
                index_list[index_name].homepath_max_data_size_mb = homepath_max_data_size_mb
                index_list[index_name].cold_path_max_data_size_mb = cold_path_max_data_size_mb

            # If the sizing results in the exact same size then do nothing in terms of configuration file updates related to sizing
            if int(max_total_data_size_mb) == int(index_list[index_name].calc_max_total_data_size_mb) and int(homepath_max_data_size_mb) == int(cur_homepath_max_data_size_mb) \
                and int(cur_cold_path_max_data_size_mb) == int(cold_path_max_data_size_mb):
                logger.debug("index=%s, found no difference to index sizing after correctly sizing home/cold paths" % (index_name))
                if indexes_requiring_changes[index_name] == "sizing":
                    del indexes_requiring_changes[index_name]
                    skipconf_file = True
                elif indexes_requiring_changes[index_name] == "bucket_sizing":
                    indexes_requiring_changes[index_name] = "bucket"
                elif indexes_requiring_changes[index_name] == "bucket":
                    pass
                else:
                    logger.warn("index=%s unhandled edge case here, continuing on, indexes_requiring_changes=\"%s\"" % (index_name, indexes_requiring_changes[index_name]))

            # Add the conf file to the list we need to work on
            if not conf_file in conf_files_requiring_changes and not skipconf_file:
                conf_files_requiring_changes.append(conf_file)
                logger.debug("index=%s, conf_file=%s now requires changes" % (index_name, conf_file))

        # The info statement just in case something goes wrong and we have to determine why (the everything statement)
        logger.info("index=%s, frozen_time_period_in_days=%s, max_total_data_size_mb=%s, avg_license_usage_per_day=%s, sizing_comment=%s, index_comp_ratio=%s, "\
                    "calculated_size=%s, estimated_days_for_current_size=%s, calc_max_total_data_size_mb=%s (after overrides), license_data_first_seen=%s, "\
                    "oldest_data_found=%s days old, rep_factor_multiplier=%s"
                    % (index_name, frozen_time_period_in_days, max_total_data_size_mb, avg_license_usage_per_day, sizing_comment, index_comp_ratio, calculated_size,
                    estimated_days_for_current_size, index_list[index_name].calc_max_total_data_size_mb, license_data_first_seen, oldest_data_found, rep_factor_multiplier))

        if index_list[index_name].calc_max_total_data_size_mb == False:
            calculated_size = 0
        else:
            calculated_size = int(index_list[index_name].calc_max_total_data_size_mb)

        # Keep the total calclated size handy within the looop
        calculated_size_total = calculated_size_total + calculated_size

        # If we have run the required checks on the index mark it True, otherwise do not, this is used later and relates to the limited index runs
        index_list[index_name].checked = True

    return conf_files_requiring_changes, indexes_requiring_changes, calculated_size_total
