#!/bin/bash

# a simple script to check if a restart may be required for a particular app/Splunk directory as per
# https://docs.splunk.com/Documentation/Splunk/latest/Indexer/Updatepeerconfigurations#Restart_or_reload_after_configuration_bundle_changes.3F
# An example run is ./check_if_restart_required.sh /opt/splunk/etc/apps/SplunkAdmins
date=`date +"%Y-%m-%d %H:%M:%S.%3N %z"`

# https://www.baeldung.com/linux/check-variable-exists-in-list
function exists_in_list() {
    LIST=$1
    DELIMITER=" "
    VALUE=$2
    echo $LIST | tr "$DELIMITER" '\n' | grep -F -q -x "$VALUE"
}

function usage {
    echo "./$(basename $0) -d --> directory to check if a restart is required (or comma separated string of directories)"
}

if [ $# -eq 0 ]; then
  usage
  exit 0
fi

while getopts "d:h" o; do
    case "$o" in
        d)
            dir=$(echo "$OPTARG" | tr ',' ' ')
            ;;
        h)
            usage
            exit 0
            ;;
        *)
            usage
            exit 1
            ;;
    esac
done

reload_conf=`grep "reload.*= simple" /opt/splunk/etc/system/default/app.conf | grep -vE "reload\.[^\.]+\." | cut -d "." -f2 | awk '{ print $1".conf" }' | sort | uniq`
# this works in my environment may require further testing...triggers with access_endpoints sometimes works but it depends what was in the config
# for example reload.distsearch          = access_endpoints /search/distributed/bundle-replication-files
# if the config file for distsearch contains [replicationBlacklist] then it won't require a restart, but if contains [replicationSettings] it may require a restart...
reload_conf="${reload_conf} authentication.conf authorize.conf collections.conf indexes.conf messages.conf props.conf transforms.conf web.conf workload_pools.conf workload_rules.conf workload_policy.conf inputs.conf restmap.conf setup.xml savedsearches.conf"

restart_required_any="False"

echo "${date} restart script begins"

echo "dir is $dir"

check_distsearch() {
    local app="$1"
    local dist_search_ignore="True"
    files=`ls ${app}/default/distsearch.conf ${app}/local/distsearch.conf 2>/dev/null`
    if [ "x$files" != "x" ]; then
        for file in `echo $files`; do
            # strip any blocks of text under the stanzas of
            # replicationWhitelist, replicationSettings:refineConf, replicationAllowlist, replicationBlacklist, replicationDenylist
            # this type of distsearch.conf should not trigger a restart
            # the grep removes the comments and counts non-empty lines
            awk '
             BEGIN {skip=0}
             /^\[(replicationWhitelist|replicationSettings:refineConf|replicationAllowlist|replicationBlacklist|replicationDenylist)\]/ {skip=1}
             /^\[/ && !/^\[(replicationWhitelist|replicationSettings:refineConf|replicationAllowlist|replicationBlacklist|replicationDenylist)\]/ { skip=0 }
             { if (skip==0) print $0 }
            ' $file | grep -v "#" | grep -vc '^$'

            exit_code=$?
            if [ ${exit_code} -eq 0 ]; then
                dist_search_ignore="False"
            fi
        done
    fi
    echo "$dist_search_ignore"
}

# if any of these files cannot be reloaded a restart is required
for app in ${dir};
do
    restart_required="False"
    default=`ls ${app}/default 2>&1 | grep -vE "No such file|data"`;
    local=`ls ${app}/local 2>&1 | grep -vE "No such file|data"`;
    combined="$default $local";
    #echo $app $combined
    # if the app has custom triggers for reload attempt to handle this scenario
    custom_app_reload_default=`grep "^reload\..*= simple" ${app}/default/app.conf 2>/dev/null| cut -d "." -f2 | awk '{ print $1".conf" }'`
    custom_app_reload_local=`grep "^reload\..*= simple" ${app}/local/app.conf 2>/dev/null| cut -d "." -f2 | awk '{ print $1".conf" }'`
    custom_app_reload="$custom_app_reload_default $custom_app_reload_local"
    for file in $combined;
    do
	echo "File is $file"
        if exists_in_list "$reload_conf" "$file"; then
            echo "${date} ${app}/$file in system/default/app.conf, reload=true"
        elif exists_in_list "$custom_app_reload" "$file"; then
            echo "${date} ${app}/$file in ${app}/app.conf, reload=true"
	    elif [ $file == "server.conf" ]; then
            count=`grep -vE "^(#|\[|\s*$)" ${app}/default/${file} ${app}/local/${file} 2>/dev/null | grep -v "conf_replication_" | wc -l`
            if [ $count -eq 0 ]; then
                echo "${date} ${app}/$file appears to have only reload config, reload=true"
            else
                echo "${date} ${app}/$file appears to have non-reload config, reload=false"
                restart_required="True"
                restart_required_any="True"
            fi
        elif [ $file == "distsearch.conf" ]; then
            result=$(check_distsearch ${app} | sed -n '2p')
            if [ $result == "False" ]; then
                echo "${date} ${app}/$file appears to have non-reload config, reload=false"
                restart_required="True"
                restart_required_any="True"
            else
                echo "${date} ${app}/$file appears to have reload config, reload=true"
	        fi
        else
            echo "${date} ${app}/$file not found, reload=false"
            restart_required="True"
            restart_required_any="True"
        fi
    done
    echo "${date} app=${app} restart_required=${restart_required}"
done

echo "${date} restart_required=${restart_required_any}"

