#!/usr/bin/env python3
#
# Router to synchronize Science Gateways Community Institute (SGCI) catalog informaton into the Warehouse Resource tables
#
# Author: Jonathan Kim, October 2021
#         JP Navarro, October 2021
#
# Resource Group:Type
#   Function
# -------------------------------------------------------------------------------------------------
# Software:Online Service
#   Write_ScienceGateways    -> from SGCI Catalog
#
import argparse
from collections import Counter
from datetime import datetime, timezone, timedelta
from hashlib import md5
import http.client as httplib
import io
import json
import logging
import logging.handlers
import os
from pid import PidFile
import pwd
import re
import shutil
import signal
import ssl
import sys, traceback
from time import sleep
from urllib.parse import urlparse
import pytz
Central = pytz.timezone("US/Central")

import django
django.setup()
from django.conf import settings as django_settings
from django.db import DataError, IntegrityError
from django.forms.models import model_to_dict
from django_markup.markup import formatter
from resource_v3.models import *
from processing_status.process import ProcessingActivity

import elasticsearch_dsl.connections
from elasticsearch import Elasticsearch, RequestsHttpConnection

import pdb

# Used during initialization before loggin is enabled
def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)

class Format_Description():
#   Initialize a Description, smart append, and render it in html using django-markup
    def __init__(self, initial=None):
        self.markup_stream = io.StringIO()
        # Docutils settings
        self.markup_settings = {'warning_stream': self.markup_stream }
        if initial is None:
            self.value = None
        else:
            clean_initial = initial.rstrip()
            if len(clean_initial) == 0:
                self.value = None
            else:
                self.value = clean_initial
    def append(self, value):
        clean_value = value.rstrip()
        if len(clean_value) > 0:
            if self.value is None:
                self.value = clean_value
            else:
                self.value += '\n{}'.format(clean_value)
    def blank_line(self): # Forced blank line used to start a markup list
        self.value += '\n'
    def html(self, ID=None): # If an ID is provided, log it to record what resource had the warnings
        if self.value is None:
            return(None)
        output = formatter(self.value, filter_name='restructuredtext', settings_overrides=self.markup_settings)
        warnings = self.markup_stream.getvalue()
        if warnings:
            logger = logging.getLogger('DaemonLog')
            if ID:
                logger.warning('markup warnings for ID: {}'.format(ID))
            for line in warnings.splitlines():
                logger.warning('markup: {}'.format(line))
        return(output)
#    def format_Description(self, input, ID):
#        output = formatter(input, filter_name='restructuredtext', settings_overrides=self.markup_settings)
#        warnings = self.markup_stream.getvalue()
#        if warnings:
#            if ID:
#                self.logger.warning('markup warnings for ID: {}'.format(ID))
#            for line in warnings.splitlines():
#                self.logger.warning('markup: {}'.format(line))
#        return(output)
    
    
class Router():
    # Initialization BEFORE we know if another self is running
    def __init__(self):
        # Parse arguments
        parser = argparse.ArgumentParser()
        parser.add_argument('--once', action='store_true', \
                            help='Run once and exit, or run continuous with sleep between interations (default)')
        parser.add_argument('--daemon', action='store_true', \
                            help='Run as daemon redirecting stdout, stderr to a file, or interactive (default)')
        parser.add_argument('-l', '--log', action='store', \
                            help='Logging level (default=warning)')
        parser.add_argument('-c', '--config', action='store', dest='config', required=True, \
                            help='Configuration file')
        parser.add_argument('--dev', action='store_true', \
                            help='Running in development environment')
        parser.add_argument('--pdb', action='store_true', \
                            help='Run with Python debugger')
        self.args = parser.parse_args()

        # Trace for debugging as early as possible
        if self.args.pdb:
            pdb.set_trace()

        # Load configuration file
        config_path = os.path.abspath(self.args.config)
        try:
            with open(config_path, 'r') as file:
                conf=file.read()
        except IOError as e:
            eprint('Error "{}" reading config={}'.format(e, config_path))
            sys.exit(1)
        try:
            self.config = json.loads(conf)
        except ValueError as e:
            eprint('Error "{}" parsing config={}'.format(e, config_path))
            sys.exit(1)

        # Configuration verification and processing
        if len(self.config['STEPS']) < 1:
            eprint('Missing config STEPS')
            sys.exit(1)
        
        if self.config.get('PID_FILE'):
            self.pidfile_path =  self.config['PID_FILE']
        else:
            name = os.path.basename(__file__).replace('.py', '')
            self.pidfile_path = '/var/run/{}/{}.pid'.format(name, name)
            
    # Setup AFTER we know that no other self is running
    def Setup(self, peak_sleep=10, offpeak_sleep=60, max_stale=24 * 60):
        # Initialize log level from arguments, or config file, or default to WARNING
        loglevel_str = (self.args.log or self.config.get('LOG_LEVEL', 'WARNING')).upper()
        loglevel_num = getattr(logging, loglevel_str, None)
        self.logger = logging.getLogger('DaemonLog')
        self.logger.setLevel(loglevel_num)
        self.formatter = logging.Formatter(fmt='%(asctime)s.%(msecs)03d %(levelname)s %(message)s', \
                                           datefmt='%Y/%m/%d %H:%M:%S')
        self.handler = logging.handlers.TimedRotatingFileHandler(self.config['LOG_FILE'], \
            when='W6', backupCount=999, utc=True)
        self.handler.setFormatter(self.formatter)
        self.logger.addHandler(self.handler)

        # Initialize stdout, stderr
        if self.args.daemon and 'LOG_FILE' in self.config:
            self.stdout_path = self.config['LOG_FILE'].replace('.log', '.daemon.log')
            self.stderr_path = self.stdout_path
            self.SaveDaemonStdOut(self.stdout_path)
            sys.stdout = open(self.stdout_path, 'wt+')
            sys.stderr = open(self.stderr_path, 'wt+')

        # Signal handling
        signal.signal(signal.SIGINT, self.exit_signal)
        signal.signal(signal.SIGTERM, self.exit_signal)

        mode =  ('daemon,' if self.args.daemon else 'interactive,') + \
            ('once' if self.args.once else 'continuous')
        self.logger.critical('Starting mode=({}), program={}, pid={}, uid={}({})'.format(mode, os.path.basename(__file__), os.getpid(), os.geteuid(), pwd.getpwuid(os.geteuid()).pw_name))

        # Connect Database
        configured_database = django_settings.DATABASES['default'].get('HOST', None)
        if configured_database:
            self.logger.critical('Warehouse database={}'.format(configured_database))
        # Django connects automatially as needed

        # Connect Elasticsearch
        if 'ELASTIC_HOSTS' in self.config:
            self.logger.critical('Warehouse elastichost={}'.format(self.config['ELASTIC_HOSTS']))
            self.ESEARCH = elasticsearch_dsl.connections.create_connection( \
                hosts = self.config['ELASTIC_HOSTS'], \
                connection_class = RequestsHttpConnection, \
                timeout = 10)
            ResourceV3Index.init()              # Initialize it if it doesn't exist
        else:
            self.logger.info('Warehouse elastichost=NONE')
            self.ESEARCH = None

        # Initialize application variables
        self.peak_sleep = peak_sleep * 60       # 10 minutes in seconds during peak business hours
        self.offpeak_sleep = offpeak_sleep * 60 # 60 minutes in seconds during off hours
        self.max_stale = max_stale * 60         # 24 hours in seconds force refresh
        self.application = os.path.basename(__file__)
        self.memory = {}                        # Used to put information in "memory"
        self.Affiliation = 'sciencegateways.org'  # This app is only for SGCI 
        self.DefaultValidity = timedelta(days = 14)

        if self.args.dev:
            self.WAREHOUSE_API_PREFIX = 'http://localhost:8000'
        else:
            self.WAREHOUSE_API_PREFIX = 'https://info.xsede.org/wh1'
        self.WAREHOUSE_API_VERSION = 'v3'
        self.WAREHOUSE_CATALOG = 'ResourceV3'

        # Used in Get_HTTP as memory cache for contents
        self.HTTP_CACHE = {}
        self.URL_USE_COUNT = {}

        # Loading all the Catalog entries for our affiliation
        self.CATALOGS = {}
        for cat in ResourceV3Catalog.objects.filter(Affiliation__exact=self.Affiliation):
            self.CATALOGS[cat.ID] = model_to_dict(cat)

        self.STEPS = []
        for stepconf in self.config['STEPS']:
            if 'CATALOGURN' not in stepconf:
                self.logger.error('Step CATALOGURN is missing or invalid')
                self.exit(1)
            if stepconf['CATALOGURN'] not in self.CATALOGS:
                self.logger.error('Step CATALOGURN is not define in Resource Catalogs')
                self.exit(1)
            myCAT = self.CATALOGS[stepconf['CATALOGURN']]
            stepconf['SOURCEURL'] = myCAT['CatalogAPIURL']
            
            # if use the same CatalogAPIURL, count and keep 
            if stepconf['SOURCEURL'] in self.URL_USE_COUNT:
                self.URL_USE_COUNT[stepconf['SOURCEURL']] += 1
            else:
                self.URL_USE_COUNT[stepconf['SOURCEURL']] = 1

            try:
                SRCURL = urlparse(stepconf['SOURCEURL'])
            except:
                self.logger.error('Step SOURCE is missing or invalid')
                self.exit(1)
            if SRCURL.scheme not in ['file', 'http', 'https']:
                self.logger.error('Source not {file, http, https}')
                self.exit(1)
            stepconf['SRCURL'] = SRCURL
            
            try:
                DSTURL = urlparse(stepconf['DESTINATION'])
            except:
                self.logger.error('Step DESTINATION is missing or invalid')
                self.exit(1)
            if DSTURL.scheme not in ['file', 'analyze', 'function', 'memory']:
                self.logger.error('Destination is not one of {file, analyze, function, memory}')
                self.exit(1)
            stepconf['DSTURL'] = DSTURL
            
            if SRCURL.scheme in ['file'] and DSTURL.scheme in ['file']:
                self.logger.error('Source and Destination can not both be a {file}')
                self.exit(1)
                
            # Merge CATALOG config and STEP config, with the latter taking precendence
            self.STEPS.append({**self.CATALOGS[stepconf['CATALOGURN']], **stepconf})

    def SaveDaemonStdOut(self, path):
        # Save daemon log file using timestamp only if it has anything unexpected in it
        try:
            file = open(path, 'r')
            lines = file.read()
            file.close()
            if not re.match("^started with pid \d+$", lines) and not re.match("^$", lines):
                ts = datetime.strftime(datetime.now(), '%Y-%m-%d_%H:%M:%S')
                newpath = '{}.{}'.format(path, ts)
                self.logger.debug('Saving previous daemon stdout to {}'.format(newpath))
                shutil.copy(path, newpath)
        except Exception as e:
            self.logger.error('Exception in SaveDaemonStdOut({})'.format(path))
        return

    def exit_signal(self, signum, frame):
        self.logger.critical('Caught signal={}({}), exiting with rc={}'.format(signum, signal.Signals(signum).name, signum))
        sys.exit(signum)
        
    def exit(self, rc):
        if rc:
            self.logger.error('Exiting with rc={}'.format(rc))
        sys.exit(rc)

    def CATALOGURN_to_URL(self, id):
        return('{}/resource-api/{}/catalog/id/{}/'.format(self.WAREHOUSE_API_PREFIX, self.WAREHOUSE_API_VERSION, id))
        
    def format_GLOBALURN(self, *args):
        newargs = list(args)
        newargs[0] = newargs[0].rstrip(':')
        return(':'.join(newargs))

    def Get_HTTP(self, url, contype):
        # return previously saved data if the source is the same 
        data_cache_key = contype + ':' + url.geturl()
        if data_cache_key in self.HTTP_CACHE:
            return({contype: self.HTTP_CACHE[data_cache_key]})

        headers = {}

        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        conn = httplib.HTTPSConnection(host=url.hostname, port=getattr(url, 'port', None), context=ctx)
 
        # Only can retrieve up to 300 SGCI data entries at once but there are more.
        # Thus, repeat the retrieving with offset and limit parameters until all the data 
        # entries are retrieved. 
        dataOffset = 0
        dataStride = 300
        lenListRetrieved = dataStride
        content = {}
        while lenListRetrieved == dataStride:
            # Add data retriveing filters on the URL path 
            url_path = url.path + '?q={}&offset={}&limit={}'.format("{'name':'sg_catalog_gateways'}", dataOffset, dataStride)
            conn.request('GET', url_path, None, headers)
            self.logger.debug('HTTP GET {}'.format(url.geturl()))
            response = conn.getresponse()
            result = response.read().decode("utf-8-sig")
            self.logger.debug('HTTP RESP {} {} (returned {}/bytes)'.format(response.status, response.reason, len(result)))
            try:
                contentRemain = json.loads(result)
                lenListRetrieved = len(contentRemain['result'])

                if dataOffset == 0: # init
                    content = contentRemain
                else: # accumulate remaining retrieved data
                    content['result'] += contentRemain['result']

                # JK_TODO: remove debug lines when done.
                print ('JK_DBG> lenListRetrieved: {} '.format(lenListRetrieved))
                dataOffset += dataStride
                print('JK_DBG> dataOffset: {} '.format(dataOffset))
                print('JK_DBG> numAllContent: {}'.format(len(content['result'])))
            except ValueError as e:
                self.logger.error('Response not in expected JSON format ({})'.format(e))
                return(None)


        # cache content only for the url used more than once
        if url.geturl() in self.URL_USE_COUNT:
            if (self.URL_USE_COUNT[url.geturl()] > 1):
                # save retrieved content to the HTTP_CACHE to reuse from memory
                self.HTTP_CACHE[data_cache_key] = content

        return({contype: content})

    def Analyze_CONTENT(self, content):
        # Write when needed
        return(0, '')

    def Write_MEMORY(self, content, contype, conkey):
        # Stores in a dictionary for reference by other processing steps
        if contype not in self.memory:
            self.memory[contype] = {}
        for item in content[contype]:
            try:
                self.memory[contype][item[conkey]] = item
            except:
                pass
        return(0, '')

    def Write_CACHE(self, file, content):
        data = json.dumps(content)
        with open(file, 'w') as my_file:
            my_file.write(data)
        self.logger.info('Serialized and wrote {} bytes to file={}'.format(len(data), file))
        return(0, '')

    def Read_CACHE(self, file, contype):
        with open(file, 'r') as my_file:
            data = my_file.read()
            my_file.close()
        try:
            content = json.loads(data)
            self.logger.info('Read and parsed {} bytes from file={}'.format(len(data), file))
            return({contype: content})
        except ValueError as e:
            self.logger.error('Error "{}" parsing file={}'.format(e, file))
            self.exit(1)

########## CUSTOMIZATIONS START ##########
    #
    # Delete old items (those in 'cur') that weren't updated (those in 'new')
    #
    def Delete_OLD(self, me, cur, new):
        for URN in [id for id in cur if id not in new]:
            try:
                ResourceV3Index.get(id = URN).delete()
            except Exception as e:
                self.logger.error('{} deleting Elastic id={}: {}'.format(type(e).__name__, URN, e))
            try:
                # TODO: uncomment this line if add relations
                #ResourceV3Relation.objects.filter(FirstResourceID__exact = URN).delete()
                ResourceV3.objects.get(pk = URN).delete()
                ResourceV3Local.objects.get(pk = URN).delete()
            except Exception as e:
                self.logger.error('{} deleting ID={}: {}'.format(type(e).__name__, URN, e))
            else:
                self.logger.info('{} deleted ID={}'.format(me, URN))
                self.STATS.update({me + '.Delete'})

    #
    # Update relations and delete relations for myURN that weren't just updated (newIDS)
    #
    def Update_REL(self, myURN, newRELATIONS):
        newIDS = []
        for relatedID in newRELATIONS:
            try:
                relationType = newRELATIONS[relatedID]
                relationHASH = md5(':'.join([relatedID, relationType]).encode('UTF-8')).hexdigest()
                relationID = ':'.join([myURN, relationHASH])
                relation = ResourceV3Relation(
                            ID = relationID,
                            FirstResourceID = myURN,
                            SecondResourceID = relatedID,
                            RelationType = relationType,
                     )
                relation.save()
            except Exception as e:
                msg = '{} saving Relation ID={}: {}'.format(type(e).__name__, relationID, e)
                self.logger.error(msg)
                return(False, msg)
            newIDS.append(relationID)
        try:
            ResourceV3Relation.objects.filter(FirstResourceID__exact = myURN).exclude(ID__in = newIDS).delete()
        except Exception as e:
            self.logger.error('{} deleting Relations for Resource ID={}: {}'.format(type(e).__name__, myURN, e))
    #
    # Log how long a processing step took
    #
    def Log_STEP(self, me):
        summary_msg = 'Processed {} in {:.3f}/seconds: {}/updates, {}/deletes, {}/skipped'.format(me,
            self.PROCESSING_SECONDS[me],
            self.STATS[me + '.Update'], self.STATS[me + '.Delete'], self.STATS[me + '.Skip'])
        self.logger.info(summary_msg)



    ########################################################################
    # Function for loading SGCI (Science Gateways Community Institute) data
    # Load SGCI data to ResourceV3 tables (local, standard)
    #
    def Write_SGCI_Gateway_Catalog(self, content, contype, config):
        start_utc = datetime.now(timezone.utc)
        myRESGROUP = 'Software'
        myRESTYPE = 'Online Service'
        me = '{} to {}({}:{})'.format(sys._getframe().f_code.co_name, self.WAREHOUSE_CATALOG, myRESGROUP, myRESTYPE)
        self.PROCESSING_SECONDS[me] = getattr(self.PROCESSING_SECONDS, me, 0)

        # For LoccalURL - make a web link to access individual data 
        localUrlPrefix = config['SRCURL'].scheme + '://' + config['SRCURL'].netloc + '/#/home/' 

        cur = {}   # Current items
        new = {}   # New items
        # get existing SGCI data from local table
        for item in ResourceV3Local.objects.filter(Affiliation__exact = self.Affiliation).filter(ID__startswith = config['URNPREFIX'].replace(":catalog:", ":resource:catalog.")):
            cur[item.ID] = item


        #------------------------------------------------------
        # iterrate data to load to Resource V3 DB tables
        #
        for item in content[contype]['result'] :
            myGLOBALURN = self.format_GLOBALURN(config['URNPREFIX'], str(item['uuid'])).replace(":catalog:", ":resource:catalog.")

            # --------------------------------------------
            # load data to ResourceV3 (local) table
            #
            try:
                local = ResourceV3Local(
                            ID = myGLOBALURN,
                            CreationTime = datetime.now(timezone.utc),
                            Validity = self.DefaultValidity,
                            Affiliation = self.Affiliation,
                            LocalID = item['uuid'],
                            LocalType = 'SGCI Catalog',
                            LocalURL = localUrlPrefix + item['uuid'],
                            CatalogMetaURL = self.CATALOGURN_to_URL(config['CATALOGURN']),
                            EntityJSON = item,
                        )
                local.save()
            except Exception as e:
                msg = '{} saving local ID={}: {}'.format(type(e).__name__, myGLOBALURN, e)
                self.logger.error(msg)
                return(False, msg)
            new[myGLOBALURN] = local


            # --------------------------------------------
            # update ResourceV3 (standard) table
            #

            # prepare for Topics and Keywords fields of the standard table
            topics = []
            keywords = []
            for category in item['value']['categories']:
                topics.append(category)
            for tag in item['value']['tags']:
                keywords.append(tag)

            # prepare for Description field. Append institutions to Description 
            institutions = item['value'].get('institutions', None)
            if institutions:
                institutions_csv =  '\nAssociated Institutions:' + ','.join(institutions)
            else: # empty or not found
                institutions_csv = ''


            try:
                resource = ResourceV3(
                            ID = myGLOBALURN,
                            Affiliation = self.Affiliation,
                            LocalID = item['uuid'],
                            QualityLevel = 'Production',
                            Name = item['value']['name'],
                            ResourceGroup = myRESGROUP,
                            Type = myRESTYPE,
                            ShortDescription = None,
                            ProviderID = None,
                            Description = item['value']['description'] + institutions_csv,
                            Topics = ','.join(topics),
                            Keywords = ','.join(keywords),
                            Audience = self.Affiliation,
                    )
                resource.save()
                if self.ESEARCH:
                    resource.indexing()
            except Exception as e:
                msg = '{} saving resource ID={}: {}'.format(type(e).__name__, myGLOBALURN, e)
                self.logger.error(msg)
                return(False, msg)

        self.Delete_OLD(me, cur, new)

        self.PROCESSING_SECONDS[me] += (datetime.now(timezone.utc) - start_utc).total_seconds()
        self.Log_STEP(me)
        return(0, '')



    ################################################################################

    def Run(self):
        while True:
            loop_start_utc = datetime.now(timezone.utc)
            self.STATS = Counter()
            self.PROCESSING_SECONDS = {}

            for stepconf in self.STEPS:
                step_start_utc = datetime.now(timezone.utc)
                pa_application = os.path.basename(__file__)
                pa_function = stepconf['DSTURL'].path
                pa_topic = stepconf['LOCALTYPE']
                pa_about = self.Affiliation
                pa_id = '{}:{}:{}:{}->{}'.format(pa_application, pa_function, pa_topic,
                    stepconf['SRCURL'].scheme, stepconf['DSTURL'].scheme)
                pa = ProcessingActivity(pa_application, pa_function, pa_id, pa_topic, pa_about)

                if stepconf['SRCURL'].scheme == 'file':
                    content = self.Read_CACHE(stepconf['SRCURL'].path, stepconf['LOCALTYPE'])
                else:
                    content = self.Get_HTTP(stepconf['SRCURL'], stepconf['LOCALTYPE'])

                if stepconf['LOCALTYPE'] not in content:
                    (rc, message) = (False, 'JSON is missing the \'{}\' element'.format(contype))
                    self.logger.error(msg)
                elif stepconf['DSTURL'].scheme == 'file':
                    (rc, message) = self.Write_CACHE(stepconf['DSTURL'].path, content)
                elif stepconf['DSTURL'].scheme == 'analyze':
                    (rc, message) = self.Analyze_CONTENT(content)
                elif stepconf['DSTURL'].scheme == 'memory':
                    (rc, message) = self.Write_MEMORY(content, stepconf['LOCALTYPE'], stepconf['DSTURL'].path)
                elif stepconf['DSTURL'].scheme == 'function':
                    (rc, message) = getattr(self, pa_function)(content, stepconf['LOCALTYPE'], stepconf)
                if not rc and message == '':  # No errors
                    message = 'Executed {} in {:.3f}/seconds'.format(pa_function,
                            (datetime.now(timezone.utc) - step_start_utc).total_seconds())
                pa.FinishActivity(rc, message)
     
            self.logger.info('Iteration duration={:.3f}/seconds'.format((datetime.now(timezone.utc) - loop_start_utc).total_seconds()))
            if self.args.once:
                break
            # Continuous
            self.smart_sleep()
        return(0)

    def smart_sleep(self):
        # Between 6 AM and 9 PM Central
        current_sleep = self.peak_sleep if 6 <= datetime.now(Central).hour <= 21 else self.offpeak_sleep
        self.logger.debug('sleep({})'.format(current_sleep))
        sleep(current_sleep)

########## CUSTOMIZATIONS END ##########

if __name__ == '__main__':
    router = Router()
    with PidFile(router.pidfile_path):
        try:
            router.Setup()
            rc = router.Run()
        except Exception as e:
            msg = '{} Exception: {}'.format(type(e).__name__, e)
            router.logger.error(msg)
            traceback.print_exc(file=sys.stdout)
            rc = 1
    router.exit(rc)
