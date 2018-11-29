#!/bin/sh

#Note this script *will* need modifications to be used for another purpose
#however this need to transfer files with resume functionality via shell script may occur again so keeping this!
#Note that SSH keys were used for the rsync transfers, so the servers in question has access to the remote server
if [ $# -lt 1 ]; then
    echo "Error need index argument, and optional bandwidth limit argument"
    exit 1
fi

indexname=$1

#Limit at which to throttle the rsync transfer
bandwidthlimit="4096"
if [ $# -gt 1 ]; then
  bandwidthlimit=$2
fi

#Force SSH key to be added to known_hosts
successFile=/tmp/successfultransferlist${indexname}.txt
tmpFile=/tmp/tmpfile${indexname}.txt

hotSourceVol=FIXME
coldSourceVol=FIXME
destVol=FIXME
host=FIXME
epochTime=FIXME

#Get a list of files to transfer
find ${hotSourceVol}/${indexname}/ -name "[dr]b*" | grep -v \.rbsentinel | grep -vE "/db$" | grep -v grep > $tmpFile 
find ${coldSourceVol}/${indexname}/ -name "[dr]b*"  | grep -v \.rbsentinel | grep -vE "/colddb$" | grep -v grep  >> $tmpFile 

#This could be done without so many mv commands but that involves checking the manuals and this is quick!
if [ -s $successFile ]; then
    sort $successFile > ${successFile}.sorted
    mv ${successFile}.sorted $successFile 
    sort $tmpFile > ${tmpFile}.sorted
    mv ${tmpFile}.sorted $tmpFile 
    comm -23 $tmpFile $successFile > ${tmpFile}.2
    mv ${tmpFile}.2 $tmpFile 
fi

#Force SSH key to be added to known_hosts
ssh -n -o "BatchMode yes" -o StrictHostKeyChecking=no -o ServerAliveInterval=30 -o ConnectTimeout=10 splunk@$desthost

destDir=${destVol}/${indexname}/`echo $i | grep -Eo "(hot|cold)\/[^/]+" | cut -d "/" -f2`/hot
ssh splunk@$desthost "mkdir -p $destDir"
for i in `cat $tmpFile`; do
    time=`echo $i | cut -d "/" -f6 | cut -d "_" -f3`
    if (( $time > epochTime )); then
        echo "`date +"%Y-%m-%d %H:%M:%S"` Dir $i is in scope for transfer, begin transfer"
        rsync -r -p -t -g --delete --exclude=.snapshots --bwlimit=$bandwidthlimit $i splunk@$desthost:$destDir
        if [ $? -eq 0 ]; then
            echo "`date +"%Y-%m-%d %H:%M:%S"` Dir $i transfer completed"
            echo $i >> $successFile 
        fi
    fi
done

