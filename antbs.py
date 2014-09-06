#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# build_pkg.py
#
# Copyright 2013 Antergos
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
#  along with this program; if not, write to the Free Software
#  Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
#  MA 02110-1301, USA.

"""Import the main class from the "Flask" library that we need to create our
web application, as well as the `render_template` function for returning
HTML files as responses and getting `request` objects that contain
information about requests that we receive, like the URL and cookies and
stuff like that."""

import ipaddress
import json
import subprocess
import time
import os
from rq import Queue
from flask import Flask, request, Response, abort, render_template
from werkzeug.contrib.fixers import ProxyFix
import requests
import docker
import src.build_pkg as builder
from src.redis_connection import db
import logging
import logging.config
import logging.handlers
import src.logging_config as logconf
import glob
from datetime import datetime, timedelta
import gevent
import gevent.monkey

gevent.monkey.patch_all()

SRC_DIR = os.path.dirname(__file__) or '.'


# Create the variable `app` which is an instance of the Flask class that
# we just imported.
app = Flask(__name__)
app.jinja_options = Flask.jinja_options.copy()
app.jinja_options['lstrip_blocks'] = True
app.jinja_options['trim_blocks'] = True
queue = Queue('build_queue', connection=db)


# Use gunicorn to proxy with nginx
app.wsgi_app = ProxyFix(app.wsgi_app)

# Setup logging
logger = logging.getLogger('')
logging.config.dictConfig(logconf.log_config)


def stream_template(template_name, **context):
    app.update_template_context(context)
    t = app.jinja_env.get_template(template_name)
    rv = t.stream(context)
    #rv.enable_buffering(5)
    rv.disable_buffering()
    return rv


def get_log_stream(bnum=None):
    #doc = docker.Client(base_url='unix://var/run/docker.sock', version='1.12', timeout=10)
    is_idle = db.get('idle')
    now_building = db.get('building')
    container = db.get('container')
    if bnum is not None:
        log = db.lrange('build_log:%s:content' % bnum, 0, -1)
        log_end = db.lrange('build_log:%s:content' % bnum, -1, -1)
        nodata = ['data: Unable to retrieve build log.\n\n']
        if not log:
            for line in nodata:
                yield line
            yield 'data: ENDOFLOG\n\n'
        else:
            last_line = False
            for line in log:
                yield 'data: %s\n\n' % line
            yield 'data: ENDOFLOG\n\n'


    else:
        nodata = ['data: There are no active builds.\n\n']
        if is_idle == "True" or now_building == "Idle" or now_building == 'Initializing...':
            app.logger.debug("No active container detected")
            for line in nodata:
                yield line
        else:
            #doclog = doc.logs(container, stdout=True, stderr=True, stream=True, timestamps=True)
            proc = subprocess.Popen(['docker', 'logs', '--follow', '-t', container], stdout=subprocess.PIPE)
            stream = iter(proc.stdout.readline, '')
            for line in stream:
                gevent.sleep(.05)
                yield 'data: %s\n\n' % line


def get_paginated(pkg_list, per_page, page):
    pkg_list.reverse()
    page -= 1
    paginated = [pkg_list[i:i + per_page] for i in range(0, len(pkg_list), per_page)]
    logger.info(paginated)
    this_page = paginated[page]
    all_pages = len(paginated)

    return this_page, all_pages


def get_build_info(page=None, status=None):
    if page is None or status is None:
        abort(500)
    try:
        all_builds = db.lrange(status, 0, -1)
    except Exception:
        all_builds = None

    pkg_list = []
    all_pages = 1

    if all_builds is not None:
        #builds, all_pages = get_paginated(all_builds, 10, page)
        for build in all_builds:
            try:
                pkg = db.get('build_log:%s:pkg' % build)
            except Exception:
                pass
            name = db.get('pkg:%s:name' % pkg)
            bnum = build
            version = db.get('pkg:%s:version' % pkg)
            start = db.get('build_log:%s:start' % bnum)
            end = db.get('build_log:%s:end' % bnum)
            all_info = dict(bnum=bnum, name=name, version=version, start=start, end=end)
            pkg_info = {bnum: all_info}
            pkg_list.append(pkg_info)
    logger.info(pkg_list)
    return pkg_list, all_pages


@app.route("/")
def homepage():
    is_idle = db.get('idle')
    check_stats = ['queue', 'completed', 'failed']
    stats = {}
    for stat in check_stats:
        res = db.llen(stat)
        stats[stat] = res
        if stat is not "queue":
            builds = db.lrange(stat, 0, -1)
            for build in builds:
                within = []
                end = db.get('build_log:%s:start' % build)
                end_fmt = datetime.strptime(end, '%m/%d/%Y %I:%M%p')
                if (datetime.now() - end_fmt) < timedelta(hours=72):
                    within.append(build)
                stats[stat] = len(within)

    x86_64 = glob.glob('/srv/antergos.info/repo/iso/testing/uefi/antergos/x86_64/*.pkg.tar.xz')
    i686 = glob.glob('/srv/antergos.info/repo/iso/testing/uefi/antergos/i686/*.pkg.tar.xz')
    stats['repo'] = len(set(x86_64 + i686))
    return render_template("overview.html", idle=is_idle, stats=stats)


@app.route("/building")
def build():
    is_idle = db.get('idle')
    now_building = db.get('building')
    cont = db.get('container')
    if cont:
        container = cont[:20]
    else:
        container = None
    bnum = db.get('building_num')
    start = db.get('building_start')

    #return render_template("building.html", idle=is_idle, building=now_building, container=container)
    return Response(stream_template('building.html', idle=is_idle, bnum=bnum, start=start, building=now_building,
                                    container=container, data=get_log_stream()))


@app.route('/get_log')
def get_log():
    bid = request.args.get('num')
    return Response(get_log_stream(bid), mimetype='text/event-stream')


@app.route('/hook', methods=['POST', 'GET'])
def hooked():
    # Store the IP address blocks that github uses for hook requests.
    hook_blocks = requests.get('https://api.github.com/meta').json()['hooks']

    if request.method == 'GET':
        return ' Nothing to see here, move along ...'

    elif request.method == 'POST':
        # Check if the POST request if from github.com
        for block in hook_blocks:
            ip = ipaddress.ip_address(u'%s' % request.remote_addr)
            if ipaddress.ip_address(ip) in ipaddress.ip_network(block):
                break  # the remote_addr is within the network range of github
        else:
            abort(403)

        if request.headers.get('X-GitHub-Event') == "ping":
            return json.dumps({'msg': 'Hi!'})
        if request.headers.get('X-GitHub-Event') != "push":
            return json.dumps({'msg': "wrong event type"})

    payload = json.loads(request.data)
    repo = payload['repository']['name']
    changes = payload['head_commit']['modified']
    added = payload['head_commit']['added']
    if repo == "antergos-packages":
        db.set('idle', 'False')
        db.set('building', "Initializing...")
        logger.info(changes)
        has_pkgs = False
        all_changes = changes + added

        for item in all_changes:
            pak = os.path.dirname(item)
            if pak is not None and pak != '':
                logger.info('Adding %s to the build queue' % pak)
                has_pkgs = True
                db.rpush('queue', pak)

        if has_pkgs:
            queue.enqueue_call(builder.handle_hook, timeout=7200)

    elif repo == "antergos-iso":
        db.set('idle', 'False')
        db.set('building', "Initializing...")
        db.set('isoFlag', 'True')
        queue.enqueue_call(builder.handle_hook, timeout=10000)

    return json.dumps({'msg': 'OK!'})


@app.route('/scheduled')
def scheduled():
    is_idle = db.get('idle')
    try:
        pkgs = db.lrange('queue', 0, -1)
    except Exception:
        pkgs = None
    building = db.get('building')
    the_queue = {}
    if pkgs is not None:
        for pak in pkgs:
            name = db.get('pkg:%s:name' % pak)
            version = db.get('pkg:%s:version' % pak)
            all_info = dict(name=name, version=version)
            the_queue[pak] = all_info

    return render_template("scheduled.html", idle=is_idle, building=building, queue=the_queue)


#@app.route('/completed/<int:page>')
@app.route('/completed')
def completed(page=None):
    is_idle = db.get('idle')
    status = 'completed'
    if page is None:
        page = 1
    building = db.get('building')
    completed, all_pages = get_build_info(page, status)

    return render_template("completed.html", idle=is_idle, building=building, completed=completed, all_pages=all_pages,
                           page=page)


@app.route('/failed/<int:page>')
@app.route('/failed')
def failed(page=None):
    is_idle = db.get('idle')
    status = 'failed'
    if page is None:
        page = 1
    building = db.get('building')
    failed, all_pages = get_build_info(page, status)

    return render_template("failed.html", idle=is_idle, building=building, failed=failed, all_pages=all_pages,
                           page=page)


@app.route('/build/<int:num>')
def build_info(num):
    pkg = db.get('build_log:%s:pkg' % num)
    ver = db.get('pkg:%s:version' % pkg)
    res = db.get('build_log:%s:result' % num)
    start = db.get('build_log:%s:start' % num)
    end = db.get('build_log:%s:end' % num)
    bnum = num
    cont = db.get('container')
    log = db.get('build_log:%s:content' % bnum)
    if cont:
        container = cont[:20]
    else:
        container = None

    return Response(stream_template("build_info.html", pkg=pkg, ver=ver, res=res, start=start, end=end,
                                    bnum=bnum, container=container, log=log))


# Some boilerplate code that just says "if you're running this from the command
# line, start here." It's not critical to know what this means yet.
if __name__ == "__main__":
    app.debug = True
    app.run(port=8020)
