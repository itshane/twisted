# -*- test-case-name: twisted.test.test_web -*-

# Twisted, the Framework of Your Internet
# Copyright (C) 2001 Matthew W. Lefkowitz
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of version 2.1 of the GNU Lesser General Public
# License as published by the Free Software Foundation.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA

"""This is a web-sever which integrates with the twisted.internet
infrastructure.
"""

# System Imports

try:
    import cStringIO as StringIO
except ImportError:
    import StringIO

import base64
import string
import socket
import types
import operator
import cgi
import copy
import time
import os
from urllib import quote
try:
    from twisted.protocols._c_urlarg import unquote
except ImportError:
    from urllib import unquote

#some useful constants
NOT_DONE_YET = 1

# Twisted Imports
from twisted.spread import pb, refpath
from twisted.internet import reactor, protocol, defer
from twisted.protocols import http
from twisted.python import log, reflect, roots, failure, components
from twisted import copyright
from twisted.cred import util
from twisted.persisted import styles

# Sibling Imports
import error
from twisted.web import util as webutil


# backwards compatability
date_time_string = http.datetimeToString
string_date_time = http.stringToDatetime

# Support for other methods may be implemented on a per-resource basis.
supportedMethods = ('GET', 'HEAD', 'POST')


class UnsupportedMethod(Exception):
    """Raised by a resource when faced with a strange request method.

    RFC 2616 (HTTP 1.1) gives us two choices when faced with this situtation:
    If the type of request is known to us, but not allowed for the requested
    resource, respond with NOT_ALLOWED.  Otherwise, if the request is something
    we don't know how to deal with in any case, respond with NOT_IMPLEMENTED.

    When this exception is raised by a Resource's render method, the server
    will make the appropriate response.

    This exception's first argument MUST be a sequence of the methods the
    resource *does* support.
    """

    allowedMethods = ()

    def __init__(self, allowedMethods, *args):
        Exception.__init__(self, allowedMethods, *args)

        if not operator.isSequenceType(allowedMethods):
            why = "but my first argument is not a sequence."
            s = ("First argument must be a sequence of"
                 " supported methods, %s" % (why,))
            raise TypeError, s


class Request(pb.Copyable, http.Request, components.Componentized):

    site = None
    appRootURL = None
    __pychecker__ = 'unusednames=issuer'

    def __init__(self, *args, **kw):
        http.Request.__init__(self, *args, **kw)
        components.Componentized.__init__(self)
        self.notifications = []

    def getStateToCopyFor(self, issuer):
        x = self.__dict__.copy()
        del x['transport']
        # XXX refactor this attribute out; it's from protocol
        # del x['server']
        del x['channel']
        del x['content']
        del x['site']
        self.content.seek(0, 0)
        x['content_data'] = self.content.read()
        x['remote'] = pb.ViewPoint(issuer, self)
        x['acqpath'] = []
        return x

    # HTML generation helpers

    def sibLink(self, name):
        "Return the text that links to a sibling of the requested resource."
        if self.postpath:
            return (len(self.postpath)*"../") + name
        else:
            return name

    def childLink(self, name):
        "Return the text that links to a child of the requested resource."
        lpp = len(self.postpath)
        if lpp > 1:
            return ((lpp-1)*"../") + name
        elif lpp == 1:
            return name
        else: # lpp == 0
            if len(self.prepath):
                return self.prepath[-1] + '/' + name
            else:
                return name

    def process(self):
        "Process a request."

        # get site from channel
        self.site = self.channel.site

        # set various default headers
        self.setHeader('server', version)
        self.setHeader('date', http.datetimeToString())
        self.setHeader('content-type', "text/html")

        # Resource Identification
        self.prepath = []
        self.postpath = map(unquote, string.split(self.path[1:], '/'))
        try:
            resrc = self.site.getResourceFor(self)
            self.render(resrc)
        except:
            self.processingFailed(failure.Failure())


    def render(self, resrc):
        try:
            body = resrc.render(self)
        except UnsupportedMethod, e:
            allowedMethods = e.allowedMethods
            if (self.method == "HEAD") and ("GET" in allowedMethods):
                # We must support HEAD (RFC 2616, 5.1.1).  If the
                # resource doesn't, fake it by giving the resource
                # a 'GET' request and then return only the headers,
                # not the body.
                log.msg("Using GET to fake a HEAD request for %s" %
                        (resrc,))
                self.method = "GET"
                body = resrc.render(self)

                if body is NOT_DONE_YET:
                    log.msg("Tried to fake a HEAD request for %s, but "
                            "it got away from me." % resrc)
                    # Oh well, I guess we won't include the content length.
                else:
                    self.setHeader('content-length', str(len(body)))

                self.write('')
                self.finish()
                return

            if self.method in (supportedMethods):
                # We MUST include an Allow header
                # (RFC 2616, 10.4.6 and 14.7)
                self.setHeader('Allow', allowedMethods)
                s = ('''Your browser approached me (at %(URI)s) with'''
                     ''' the method "%(method)s".  I only allow'''
                     ''' the method%(plural)s %(allowed) here.''' % {
                    'URI': self.uri,
                    'method': self.method,
                    'plural': ((len(allowedMethods) > 1) and 's') or '',
                    'allowed': string.join(allowedMethods, ', ')
                    })
                epage = error.ErrorPage(http.NOT_ALLOWED,
                                        "Method Not Allowed", s)
                body = epage.render(self)
            else:
                epage = error.ErrorPage(http.NOT_IMPLEMENTED, "Huh?",
                                        """I don't know how to treat a"""
                                        """ %s request."""
                                        % (self.method))
                body = epage.render(self)
        # end except UnsupportedMethod

        if body == NOT_DONE_YET:
            return
        if type(body) is not types.StringType:
            body = error.ErrorPage(http.INTERNAL_SERVER_ERROR,
                "Request did not return a string",
                "Request: "+html.PRE(reflect.safe_repr(self))+"<br />"+
                "Resource: "+html.PRE(reflect.safe_repr(resrc))+"<br />"+
                "Value: "+html.PRE(reflect.safe_repr(body))).render(self)

        if self.method == "HEAD":
            if len(body) > 0:
                # This is a Bad Thing (RFC 2616, 9.4)
                log.msg("Warning: HEAD request %s for resource %s is"
                        " returning a message body."
                        "  I think I'll eat it."
                        % (self, resrc))
                self.setHeader('content-length', str(len(body)))
            self.write('')
        else:
            self.setHeader('content-length', str(len(body)))
            self.write(body)
        self.finish()

    def processingFailed(self, reason):
        log.err(reason)
        body = ("<html><head><title>web.Server Traceback (most recent call last)</title></head>"
                "<body><b>web.Server Traceback (most recent call last):</b>\n\n"
                "%s\n\n</body></html>\n"
                % webutil.formatFailure(reason))
        self.setResponseCode(http.INTERNAL_SERVER_ERROR)
        self.setHeader('content-type',"text/html")
        self.setHeader('content-length', str(len(body)))
        self.write(body)
        self.finish()
        return reason

    def notifyFinish(self):
        self.notifications.append(defer.Deferred())
        return self.notifications[-1]

    def connectionLost(self, reason):
        for d in self.notifications:
            d.errback(reason)
        self.notifications = []

    def finish(self):
        http.Request.finish(self)
        for d in self.notifications:
            d.callback(None)
        self.notifications = []

    def view_write(self, issuer, data):
        """Remote version of write; same interface.
        """
        self.write(data)

    def view_finish(self, issuer):
        """Remote version of finish; same interface.
        """
        self.finish()

    def view_addCookie(self, issuer, k, v, **kwargs):
        """Remote version of addCookie; same interface.
        """
        self.addCookie(k, v, **kwargs)

    def view_setHeader(self, issuer, k, v):
        """Remote version of setHeader; same interface.
        """
        self.setHeader(k, v)

    def view_setLastModified(self, issuer, when):
        """Remote version of setLastModified; same interface.
        """
        self.setLastModified(when)

    def view_setETag(self, issuer, tag):
        """Remote version of setETag; same interface.
        """
        self.setETag(tag)

    def view_setResponseCode(self, issuer, code):
        """Remote version of setResponseCode; same interface.
        """
        self.setResponseCode(code)

    def view_registerProducer(self, issuer, producer, streaming):
        """Remote version of registerProducer; same interface.
        (requires a remote producer.)
        """
        self.registerProducer(_RemoteProducerWrapper(producer), streaming)

    def view_unregisterProducer(self, issuer):
        self.unregisterProducer()

    ### these calls remain local

    session = None

    def getSession(self, sessionInterface = None):
        # Session management
        if not self.session:
            cookiename = string.join(['TWISTED_SESSION'] + self.sitepath, "_")
            sessionCookie = self.getCookie(cookiename)
            if sessionCookie:
                try:
                    self.session = self.site.getSession(sessionCookie)
                except KeyError:
                    pass
            # if it still hasn't been set, fix it up.
            if not self.session:
                self.session = self.site.makeSession()
                self.addCookie(cookiename, self.session.uid, path='/')
        self.session.touch()
        if sessionInterface:
            return self.session.getComponent(sessionInterface)
        return self.session

    def prePathURL(self):
        inet, addr, port = self.getHost()
        if port == 80:
            hostport = ''
        else:
            hostport = ':%d' % port
        return quote('http%s://%s%s/%s' % (
            self.isSecure() and 's' or '',
            self.getRequestHostname(),
            hostport,
            string.join(self.prepath, '/')), "/:")

    def rememberRootURL(self):
        """
        Remember the currently-processed part of the URL for later
        recalling.
        """
        self.appRootURL = self.prePathURL()

    def getRootURL(self):
        """
        Get a previously-remembered URL.
        """
        return self.appRootURL

class _RemoteProducerWrapper:
    def __init__(self, remote):
        self.resumeProducing = remote.remoteMethod("resumeProducing")
        self.pauseProducing = remote.remoteMethod("pauseProducing")
        self.stopProducing = remote.remoteMethod("stopProducing")


class Session(components.Componentized):
    """A user's session with a system.

    This utility class contains no functionality, but is used to
    represent a session.
    """
    def __init__(self, site, uid):
        """Initialize a session with a unique ID for that session.
        """
        components.Componentized.__init__(self)
        self.site = site
        self.uid = uid
        self.expireCallbacks = []
        self.touch()
        self.sessionNamespaces = {}

    def notifyOnExpire(self, callback):
        """Call this callback when the session expires or logs out.
        """
        self.expireCallbacks.append(callback)

    def expire(self):
        """Expire/logout of the session.
        """
        #log.msg("expired session %s" % self.uid)
        del self.site.sessions[self.uid]
        for c in self.expireCallbacks:
            c()
        self.expireCallbacks = []

    def touch(self):
        self.lastModified = time.time()

    def checkExpired(self):
        # If I haven't been touched in 15 minutes:
        if time.time() - self.lastModified > 900:
            if self.site.sessions.has_key(self.uid):
                self.expire()
            else:
                pass
                #log.msg("no session to expire: %s" % self.uid)
        else:
            #log.msg("session given the will to live for 30 more minutes")
            reactor.callLater(1800, self.checkExpired)

version = "TwistedWeb/%s" % copyright.version


class Site(http.HTTPFactory):

    counter = 0
    requestFactory = Request

    def __init__(self, resource, logPath=None):
        """Initialize.
        """
        http.HTTPFactory.__init__(self, logPath=logPath)
        self.sessions = {}
        self.resource = resource

    def _openLogFile(self, path):
        from twisted.python import logfile
        return logfile.LogFile(os.path.basename(path), os.path.dirname(path))

    def __getstate__(self):
        d = self.__dict__.copy()
        d['sessions'] = {}
        return d

    def _mkuid(self):
        """(internal) Generate an opaque, unique ID for a user's session.
        """
        import md5, random
        self.counter = self.counter + 1
        return md5.new("%s_%s" % (str(random.random()) , str(self.counter))).hexdigest()

    def makeSession(self):
        """Generate a new Session instance, and store it for future reference.
        """
        uid = self._mkuid()
        s = Session(self, uid)
        session = self.sessions[uid] = s
        reactor.callLater(1800, s.checkExpired)
        return session

    def getSession(self, uid):
        """Get a previously generated session, by its unique ID.
        This raises a KeyError if the session is not found.
        """
        return self.sessions[uid]

    def buildProtocol(self, addr):
        """Generate a channel attached to this site.
        """
        channel = http.HTTPFactory.buildProtocol(self, addr)
        channel.requestFactory = self.requestFactory
        channel.site = self
        return channel

    isLeaf = 0

    def render(self, request):
        """Redirect because a Site is always a directory.
        """
        request.redirect(request.prePathURL() + '/')
        request.finish()

    def getChildWithDefault(self, pathEl, request):
        """Emulate a resource's getChild method.
        """
        request.site = self
        return self.resource.getChildWithDefault(pathEl, request)

    def getResourceFor(self, request):
        """Get a resource for a request.

        This iterates through the resource heirarchy, calling
        getChildWithDefault on each resource it finds for a path element,
        stopping when it hits an element where isLeaf is true.
        """
        request.site = self
        # Sitepath is used to determine cookie names between distributed
        # servers and disconnected sites.
        request.sitepath = copy.copy(request.prepath)
        request.acqpath = copy.copy(request.prepath)
        return self.resource.getChildForRequest(request)


import html
