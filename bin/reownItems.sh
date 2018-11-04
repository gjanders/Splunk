#!/bin/sh

if [ $# -lt 2 ]; then
  echo "Please pass in the username to work on"
  echo "Please pass in the 2nd username to own the items to"
  echo "Please pass in a 3rd argument to actually run otherwise this runs in debug mode"
fi

debugMode="true"
if [ $# -eq 3 ]; then
  debugMode="false"
fi

username="$1"
newOwner="$2"
grep -R $username /opt/splunk/etc/* | grep -v "\.js" | grep -E "\.meta|\.conf" | cut -d ":" -f1 | sort | uniq > /tmp/allFilesFoundToReown.txt
for aFile in `cat /tmp/allFilesFoundToReown.txt`; do
  echo $aFile
  #Hardcoding because /opt/splunk/etc/apps/<appName>
  type=`echo $aFile | cut -d "/" -f 5`

  app=""
  if [ "$type" = "users" ]; then
    app=`echo $aFile | cut -d "/" -f 7`
  else
    app=`echo $aFile | cut -d "/" -f 6`
  fi

  #Extract the lines for [views/...] or similar and combine it with the "owner = " line somewhere below it if it should exist
  #Then remove the [ ] around the view/props/savedsearch
  grep -E "^\[|owner" $aFile | sed -e ':a' -e 'N' -e '$!ba' -e 's/\nowner/ owner/g' | grep $username | cut -d "]" -f1 | cut -d "[" -f2 > /tmp/allEntitiesToReown.txt

  #For each entity we have to reown them
  for entity in `cat /tmp/allEntitiesToReown.txt`; do
    entityType=`echo $entity | cut -d "/" -f1`
    entityName=`echo $entity | cut -d "/" -f2`
    entityName2=`echo $entity | cut -d "/" -f3`

    echo "app is $app and owner is $newOwner"
    if [ "$entityType" = "savedsearches" ] ; then
        echo "Saved search"
        sharing=`$SPLUNK_HOME/bin/splunk _internal call "/servicesNS/$newOwner/$app/saved/searches/$entityName" | grep sharing | cut -d ">" -f2 | cut -d "<" -f1`
        echo $SPLUNK_HOME/bin/splunk _internal call "/servicesNS/$newOwner/$app/saved/searches/$entityName/acl" -post:owner $newOwner -post:sharing $sharing
        if [ $debugMode = "false" ]; then
            $SPLUNK_HOME/bin/splunk _internal call "/servicesNS/$newOwner/$app/saved/searches/$entityName/acl" -post:owner $newOwner -post:sharing $sharing
        fi
    elif [ "$entityType" = "views" ] ; then
        echo "view type"
        sharing=`$SPLUNK_HOME/bin/splunk _internal call "/servicesNS/$newOwner/$app/data/ui/views/$entityName" | grep sharing | cut -d ">" -f2 | cut -d "<" -f1`
        echo $SPLUNK_HOME/bin/splunk _internal call "/servicesNS/$newOwner/$app/data/ui/views/$entityName/acl" -post:owner $newOwner -post:sharing $sharing
        if [ $debugMode = "false" ]; then
            $SPLUNK_HOME/bin/splunk _internal call "/servicesNS/$newOwner/$app/data/ui/views/$entityName/acl" -post:owner $newOwner -post:sharing $sharing
        fi
    #Props are 3 level deep
    elif [ "$entityType" = "props" ] ; then
        echo "props type"
        echo $SPLUNK_HOME/bin/splunk _internal call "/servicesNS/$newOwner/$app/data/props/extractions/$entityName%20%3A%20$entityName2"
        sharing=`$SPLUNK_HOME/bin/splunk _internal call "/servicesNS/$newOwner/$app/data/props/extractions/$entityName%20%3A%20$entityName2" | grep sharing | cut -d ">" -f2 | cut -d "<" -f1`
        #echo `$SPLUNK_HOME/bin/splunk _internal call "/servicesNS/$newOwner/$app/data/props/extractions/$entityName%20%3A%20$entityName2"`
        echo $SPLUNK_HOME/bin/splunk _internal call "/servicesNS/$newOwner/$app/data/props/extractions/$entityName%20%3A%20$entityName2/acl" -post:owner $newOwner -post:sharing $sharing
        if [ $debugMode = "false" ]; then
            $SPLUNK_HOME/bin/splunk _internal call "/servicesNS/$newOwner/$app/data/props/extractions/$entityName%20%3A%20$entityName2/acl" -post:owner $newOwner -post:sharing $sharing
        fi
    elif [ "$entityType" = "datamodels" ]; then
        echo "data models"
        echo $SPLUNK_HOME/bin/splunk _internal call "/servicesNS/nobody/$app/data/models/$entityName/acl" -post:owner $newOwner -post:sharing $sharing
        if [ $debugMode = "false" ]; then
            $SPLUNK_HOME/bin/splunk _internal call "/servicesNS/nobody/$app/data/models/$entityName/acl" -post:owner $newOwner -post:sharing $sharing
        fi
    fi
  done
done
