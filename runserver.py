#!/usr/bin/python
# -*- coding: utf-8 -*-

import os
import sys
import shutil
import logging
import time
import re

from distutils.version import StrictVersion

from threading import Thread, Event
from queue import Queue
from flask_cors import CORS
from flask_cache_bust import init_cache_busting

from pogom import config
from pogom.app import Pogom
from pogom.utils import get_args, insert_mock_data, get_encryption_lib_path

from pogom.search import search_overseer_thread, fake_search_loop
from pogom.models import init_database, create_tables, drop_tables

# Currently supported pgoapi
pgoapi_version = "1.1.7"

# Moved here so logger is configured at load time
logging.basicConfig(format='%(asctime)s [%(threadName)16s][%(module)14s][%(levelname)8s] %(message)s')
log = logging.getLogger()

# Make sure pogom/pgoapi is actually removed if it is an empty directory
# This is a leftover directory from the time pgoapi was embedded in PokemonGo-Map
# The empty directory will cause problems with `import pgoapi` so it needs to go
oldpgoapiPath = os.path.join(os.path.dirname(__file__), "pogom/pgoapi")
if os.path.isdir(oldpgoapiPath):
    log.info("I found %s, but its no longer used. Going to remove it...", oldpgoapiPath)
    shutil.rmtree(oldpgoapiPath)
    log.info("Done!")

# Assert pgoapi is installed
try:
    import pgoapi
    from pgoapi import utilities as util
except ImportError:
    log.critical("It seems `pgoapi` is not installed. You must run pip install -r requirements.txt again")
    sys.exit(1)

# Assert pgoapi >= pgoapi_version
if not hasattr(pgoapi, "__version__") or StrictVersion(pgoapi.__version__) < StrictVersion(pgoapi_version):
    log.critical("It seems `pgoapi` is not up-to-date. You must run pip install -r requirements.txt again")
    sys.exit(1)

if __name__ == '__main__':
    # Check if we have the proper encryption library file and get its path
    encryption_lib_path = get_encryption_lib_path()
    if encryption_lib_path is "":
        sys.exit(1)

    args = get_args()

    if args.debug:
        log.setLevel(logging.DEBUG)
    else:
        log.setLevel(logging.INFO)

    # Let's not forget to run Grunt / Only needed when running with webserver
    if not args.no_server:
        if not os.path.exists(os.path.join(os.path.dirname(__file__), 'static/dist')):
            log.critical('Missing front-end assets (static/dist) -- please run "npm install && npm run build" before starting the server')
            sys.exit()

    # These are very noisey, let's shush them up a bit
    logging.getLogger('peewee').setLevel(logging.INFO)
    logging.getLogger('requests').setLevel(logging.WARNING)
    logging.getLogger('pgoapi.pgoapi').setLevel(logging.WARNING)
    logging.getLogger('pgoapi.rpc_api').setLevel(logging.INFO)
    logging.getLogger('werkzeug').setLevel(logging.ERROR)

    config['parse_pokemon'] = not args.no_pokemon
    config['parse_pokestops'] = not args.no_pokestops
    config['parse_gyms'] = not args.no_gyms

    # Turn these back up if debugging
    if args.debug:
        logging.getLogger('requests').setLevel(logging.DEBUG)
        logging.getLogger('pgoapi').setLevel(logging.DEBUG)
        logging.getLogger('rpc_api').setLevel(logging.DEBUG)

    # use lat/lng directly if matches such a pattern
    prog = re.compile("^(-?\d+\.\d+),?\s?(-?\d+\.\d+)$")
    parsed_locations = args.location.split("|")
    locations = []
    for location in parsed_locations:
        res = prog.match(location)
        position = None
        if res:
            log.debug('Using coordinates from CLI directly')
            position = PogoWorker(float(res.group(1)), float(res.group(2)), 0)
        else:
            log.debug('Looking up coordinates in API')
            position = PogoWorker(*util.get_pos_by_name(args.location))
        locations.append(position)

    # add accounts to locations
    if len(args.accounts) < len(locations):
        log.warning('Provided fever accounts than locations. Removing extra locations..')
        while len(args.accounts) < len(locations):
            locations.pop()

    for i, account in enumerate(args.accounts):
        locations[i].add_account(account)

    if not locations:
        log.error('Could not initialize locations, aborting')
        sys.exit()

    if args.no_pokemon:
        log.info('Parsing of Pokemon disabled')
    if args.no_pokestops:
        log.info('Parsing of Pokestops disabled')
    if args.no_gyms:
        log.info('Parsing of Gyms disabled')

    config['LOCALE'] = args.locale
    config['CHINA'] = args.china

    app = Pogom(__name__)
    db = init_database(app)
    if args.clear_db:
        log.info('Clearing database')
        if args.db_type == 'mysql':
            drop_tables(db)
        elif os.path.isfile(args.db):
            os.remove(args.db)
    create_tables(db)

    app.set_locations(locations)

    # Control the search status (running or not) across threads
    pause_bit = Event()
    pause_bit.clear()

    if not args.only_server:
        # Gather the pokemons!
        if not args.mock:
            log.debug('Starting a real search thread')
            search_thread = Thread(target=search_overseer_thread,
                                   args=(args, locations, pause_bit, encryption_lib_path))
        else:
            log.debug('Starting a fake search thread')
            insert_mock_data(locations[0])
            search_thread = Thread(target=fake_search_loop)

        search_thread.daemon = True
        search_thread.name = 'search_thread'
        search_thread.start()

    if args.cors:
        CORS(app)

    # No more stale JS
    init_cache_busting(app)

    app.set_search_control(pause_bit)

    config['ROOT_PATH'] = app.root_path
    config['GMAPS_KEY'] = args.gmaps_key

    if args.no_server:
        # This loop allows for ctrl-c interupts to work since flask won't be holding the program open
        while search_thread.is_alive():
            time.sleep(60)
    else:
        app.run(threaded=True, use_reloader=False, debug=args.debug, host=args.host, port=args.port)
