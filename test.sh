#!/usr/bin/env sh

uwsgi -H doomer --http :8080 --wsgi-file doomerwaver.py --http-timeout 1800
