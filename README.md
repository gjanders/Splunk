# Splunk
Other Splunk scripts which do not fit into the SplunkAdmins application

Limited documentation is available for the transfer splunk knowledge objects script below

## Purpose?
Transfer Splunk knowledge objects from 1 search head to another without going through the deployer (except for lookup files, they cannot be created via REST API as such) 

## How?
A custom written python script that can be run via: 

```splunk cmd python transfersplunkknowledgeobjects.py```

## Why?

Creating knowledge objects and pushing them out via the deployer is frustrating as they cannot be deleted from the search head, they must now be removed from the deployer. 

Splunk does not supply any scripts for search head to search head knowledge object transfer so I've created one. While you can copy from 1 search head cluster to another search head cluster you either need to trigger the replication of the knowledge object (via a save) or restart all search head cluster members with a copy of the data, this script avoids these issues by working through the REST API

## Example usage
```splunk cmd python transfersplunkknowledgeobjects.py -srcURL "https://thesourceserver:8089" -destURL "https://localhost:8089" -srcUsername "admin" -srcPassword "removed" -srcApp sourceAppName -destUsername "admin" -destPassword "changeme" -destApp destAppName -printPasswords -all```

This transfers knowledge objects from thesourceserver to localhost with the username of admin, it also uses the login of "admin" on the localhost to create the new objects (note that the objects are created with the same owner as the source server unless the ```-destOwner``` option is used)

The source application on "thesourceserver" is sourceAppName and the destination app on the localhost server is destAppName 

If any issues are encountered a ```-debugMode``` switch will supply additional logging, also note that the ```-all``` switch is used to transfer all knowledge objects, if you do not wish to transfer all then you can send in a controlled list of object types ```-tags, -macros, -eventtypes``` or similar 

Here's an example of transferring one specific users objects over and re-owning them to a new owner: 

```splunk cmd python transfersplunkknowledgeobjects.py -srcURL "https://thesourceserver:8089" -destURL "https://localhost:8089" -srcUsername "admin" -srcPassword "removed" -srcApp sourceAppName -includeOwner aSpecificIndividual -destOwner <another username to own the objects>```

## Parameters

```splunk cmd python transfersplunkknowledgeobjects.py -h ```

Provides the full list, a summary of the main options are below 

### Controlling which knowledge objects are transferred
```-all``` (optional) migrate all knowledge objects  
```-macros``` (optional) migrate macro knowledge objects  
```-tags``` (optional) migrate tag knowledge objects  
```-eventtypes``` (optional) migrate event types knowledge objects  
```-allFieldRelated``` (optional) migrate all objects under fields  
```-calcFields``` (optional) migrate calc fields knowledge objects  
```-fieldAlias``` (optional) migrate field alias knowledge objects  
```-fieldExtraction``` (optional) migrate field extraction knowledge objects  
```-fieldTransforms``` (optional) migrate field transformation knowledge objects  
```-lookupDefinition``` (optional) migrate lookup definition knowledge objects  
```-workflowActions``` (optional) migrate workflow actions  
```-sourcetypeRenaming``` (optional) migrate sourcetype renaming  
```-automaticLookup``` (optional) migrate automation lookup knowledge objects  
```-datamodels``` (optional) migrate data model knowledge objects  
```-dashboards``` (optional) migrate dashboards (user interface -> views)  
```-savedsearches``` (optional) migrate saved search objects (this includes reports/alerts)  
```-navMenu``` (optional) migrate navigation menus  
```-navMenuWithDefaultOverride``` (optional) override the default nav menu in the destination app  
```-viewstates``` (optional) migrate viewstates  
```-times``` (optional) migrate time labels (conf-times)  
```-collections``` (optional) migrate collections (kvstore collections)  
```-panels``` (optional) migrate pre-built dashboard panels  

### Filtering
```-noPrivate``` (optional) disable the migration of user level / private objects  
```-noDisabled``` (optional) disable the migratio of objects with a disabled status in Splunk  
```-includeEntities``` INCLUDEENTITIES comma separated list of object values to include (double quoted)  
```-excludeEntities``` EXCLUDEENTITIES comma separated list of object values to exclude (double quoted)  
```-includeOwner``` INCLUDEOWNER comma separated list of owners objects that should be transferred (double quoted)  
```-excludeOwner``` EXCLUDEOWNER comma separated list of owners objects that should be transferred (double quoted)  
```-privateOnly``` PRIVATEONLY Only transfer private objects  

### Logging options
```-debugMode``` (optional) turn on DEBUG level logging (defaults to INFO)  
```-printPasswords``` (optional) print passwords in the log files (dev only)  

### Other ###
```-ignoreViewstatesAttribute``` (optional) when creating saved searches strip the vsid parameter/attribute before attempting to create the saved search  
```-disableAlertsOrReportsOnMigration``` (optional) when creating alerts/reports, set disabled=1 (or enableSched=0) irrelevant of the previous setting pre-migration
