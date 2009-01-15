#!/usr/bin/env python

## Copyright (C) 2007, 2008, 2009 Tim Waugh <twaugh@redhat.com>
## Copyright (C) 2007, 2008, 2009 Red Hat, Inc.

## This program is free software; you can redistribute it and/or modify
## it under the terms of the GNU General Public License as published by
## the Free Software Foundation; either version 2 of the License, or
## (at your option) any later version.

## This program is distributed in the hope that it will be useful,
## but WITHOUT ANY WARRANTY; without even the implied warranty of
## MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
## GNU General Public License for more details.

## You should have received a copy of the GNU General Public License
## along with this program; if not, write to the Free Software
## Foundation, Inc., 675 Mass Ave, Cambridge, MA 02139, USA.

import threading
import cups
import gobject
import gtk
from debug import *

_ = lambda x: x
N_ = lambda x: x
def set_gettext_function (fn):
    global _
    _ = fn

class AuthDialog(gtk.Dialog):
    AUTH_FIELD={'username': N_("Username:"),
                'password': N_("Password:"),
                'domain': N_("Domain:")}

    def __init__ (self, title=None, parent=None,
                  flags=gtk.DIALOG_MODAL | gtk.DIALOG_NO_SEPARATOR,
                  buttons=(gtk.STOCK_CANCEL, gtk.RESPONSE_CANCEL,
                           gtk.STOCK_OK, gtk.RESPONSE_OK),
                  auth_info_required=['username', 'password'],
                  allow_remember=False):
        if title == None:
            title = _("Authentication")
        gtk.Dialog.__init__ (self, title, parent, flags, buttons)
        self.auth_info_required = auth_info_required
        self.set_default_response (gtk.RESPONSE_OK)
        self.set_border_width (6)
        self.set_resizable (False)
        hbox = gtk.HBox (False, 12)
        hbox.set_border_width (6)
        image = gtk.Image ()
        image.set_from_stock (gtk.STOCK_DIALOG_AUTHENTICATION,
                              gtk.ICON_SIZE_DIALOG)
        image.set_alignment (0.0, 0.0)
        hbox.pack_start (image, False, False, 0)
        vbox = gtk.VBox (False, 12)
        self.prompt_label = gtk.Label ()
        vbox.pack_start (self.prompt_label, False, False, 0)

        num_fields = len (auth_info_required)
        table = gtk.Table (num_fields, 2)
        table.set_row_spacings (6)
        table.set_col_spacings (6)

        self.field_entry = []
        for i in range (num_fields):
            field = auth_info_required[i]
            label = gtk.Label (_(self.AUTH_FIELD.get (field, field)))
            label.set_alignment (0, 0.5)
            table.attach (label, 0, 1, i, i + 1)
            entry = gtk.Entry ()
            entry.set_visibility (field != 'password')
            table.attach (entry, 1, 2, i, i + 1, 0, 0)
            self.field_entry.append (entry)

        self.field_entry[num_fields - 1].set_activates_default (True)
        vbox.pack_start (table, False, False, 0)
        hbox.pack_start (vbox, False, False, 0)
        self.vbox.pack_start (hbox)

        if allow_remember:
            cb = gtk.CheckButton (_("Remember password"))
            cb.set_active (False)
            vbox.pack_start (cb)
            self.remember_checkbox = cb

        self.vbox.show_all ()

    def set_prompt (self, prompt):
        self.prompt_label.set_markup ('<span weight="bold" size="larger">' +
                                      prompt + '</span>')
        self.prompt_label.set_use_markup (True)
        self.prompt_label.set_alignment (0, 0)
        self.prompt_label.set_line_wrap (True)

    def set_auth_info (self, auth_info):
        for i in range (len (self.field_entry)):
            self.field_entry[i].set_text (auth_info[i])

    def get_auth_info (self):
        return map (lambda x: x.get_text (), self.field_entry)

    def get_remember_password (self):
        try:
            return self.remember_checkbox.get_active ()
        except AttributeError:
            return False

    def field_grab_focus (self, field):
        i = self.auth_info_required.index (field)
        self.field_entry[i].grab_focus ()

class Connection:
    def __init__ (self, parent=None, try_as_root=True, lock=False,
                  host=None, port=None, encryption=None):
        if host != None:
            cups.setServer (host)
        if port != None:
            cups.setPort (port)
        if encryption != None:
            cups.setEncryption (encryption)

        self._use_password = ''
        self._parent = parent
        self._try_as_root = try_as_root
        self._use_user = cups.getUser ()
        self._server = cups.getServer ()
        self._port = cups.getPort()
        self._encryption = cups.getEncryption ()
        self._connect ()
        self._prompt_allowed = True
        self._operation_stack = []
        self._lock = lock
        self._gui_event = threading.Event ()

    def _begin_operation (self, operation):
        self._operation_stack.append (operation)

    def _end_operation (self):
        self._operation_stack.pop ()

    def _get_prompt_allowed (self, ):
        return self._prompt_allowed

    def _set_prompt_allowed (self, allowed):
        self._prompt_allowed = allowed

    def _set_lock (self, whether):
        self._lock = whether

    def _connect (self):
        cups.setUser (self._use_user)
        self._connection = cups.Connection (host=self._server,
                                            port=self._port,
                                            encryption=self._encryption)
        self._user = self._use_user
        debugprint ("Connected as user %s" % self._user)
        methodtype = type (self._connection.getPrinters)
        for fname in dir (self._connection):
            if fname[0] == '_':
                continue
            fn = getattr (self._connection, fname)
            if type (fn) != methodtype:
                continue
            setattr (self, fname, self._make_binding (fname, fn))

    def _make_binding (self, fname, fn):
        return lambda *args, **kwds: self._authloop (fname, fn, *args, **kwds)

    def _authloop (self, fname, fn, *args, **kwds):
        self._passes = 0
        c = self._connection
        retry = False
        while retry or self._perform_authentication () != 0:
            if c != self._connection:
                # We have reconnected.
                fn = getattr (self._connection, fname)
                c = self._connection

            cups.setUser (self._use_user)

            try:
                result = fn.__call__ (*args, **kwds)

                if fname == 'adminGetServerSettings':
                    # Special case for a rubbish bit of API.
                    if result == {}:
                        # Authentication failed, but we aren't told that.
                        raise cups.IPPError (cups.IPP_NOT_AUTHORIZED, '')
                break
            except cups.IPPError, (e, m):
                if not self._cancel and (e == cups.IPP_NOT_AUTHORIZED or
                                         e == cups.IPP_FORBIDDEN):
                    self._failed (e == cups.IPP_FORBIDDEN)
                elif not self._cancel and e == cups.IPP_SERVICE_UNAVAILABLE:
                    if self._lock:
                        self._gui_event.clear ()
                        gobject.timeout_add (1, self._ask_retry_server_error, m)
                        self._gui_event.wait ()
                    else:
                        self._ask_retry_server_error (m)

                    if self._retry_response == gtk.RESPONSE_OK:
                        debugprint ("retrying operation...")
                        retry = True
                        self._passes -= 1
                    else:
                        self._cancel = True
                        raise
                else:
                    if self._cancel and not self._cannot_auth:
                        raise cups.IPPError (0, _("Operation canceled"))

                    raise
            except cups.HTTPError, (s,):
                if not self._cancel and (s == cups.HTTP_UNAUTHORIZED or
                                         s == cups.HTTP_FORBIDDEN):
                    self._failed (s == cups.HTTP_FORBIDDEN)
                else:
                    raise

        return result

    def _ask_retry_server_error (self, message):
        if self._lock:
            gtk.gdk.threads_enter ()

        d = gtk.MessageDialog (self._parent,
                               gtk.DIALOG_MODAL |
                               gtk.DIALOG_DESTROY_WITH_PARENT,
                               gtk.MESSAGE_ERROR,
                               gtk.BUTTONS_NONE,
                               _("CUPS server error (%s)") %
                               self._operation_stack[0])
        d.format_secondary_text (_("There was an error during the "
                                   "CUPS operation: '%s'." % message))
        d.add_buttons (gtk.STOCK_CANCEL, gtk.RESPONSE_CANCEL,
                       _("Retry"), gtk.RESPONSE_OK)
        d.set_default_response (gtk.RESPONSE_OK)
        if self._lock:
            d.connect ("response", self._on_retry_server_error_response)
            gtk.gdk.threads_leave ()
        else:
            self._retry_response = d.run ()
            d.destroy ()

    def _on_retry_server_error_response (self, dialog, response):
        self._retry_response = response
        dialog.destroy ()
        self._gui_event.set ()

    def _failed (self, forbidden=False):
        self._has_failed = True
        self._forbidden = forbidden

    def _password_callback (self, prompt):
        debugprint ("Got password callback")
        if self._cancel or self._auth_called:
            return ''

        self._auth_called = True
        self._prompt = prompt
        return self._use_password

    def _perform_authentication (self):
        self._passes += 1

        debugprint ("Authentication pass: %d" % self._passes)
        if self._passes == 1:
            # Haven't yet tried the operation.  Set the password
            # callback and return > 0 so we try it for the first time.
            self._has_failed = False
            self._forbidden = False
            self._auth_called = False
            self._cancel = False
            self._cannot_auth = False
            self._dialog_shown = False
            cups.setPasswordCB (self._password_callback)
            debugprint ("Authentication: password callback set")
            return 1

        debugprint ("Forbidden: %s" % self._forbidden)
        if not self._has_failed:
            # Tried the operation and it worked.  Return 0 to signal to
            # break out of the loop.
            debugprint ("Authentication: Operation successful")
            return 0

        # Reset failure flag.
        self._has_failed = False

        if self._passes >= 2:
            # Tried the operation without a password and it failed.
            if (self._try_as_root and
                self._user != 'root' and
                (self._server[0] == '/' or self._forbidden)):
                # This is a UNIX domain socket connection so we should
                # not have needed a password (or it is not a UDS but
                # we got an HTTP_FORBIDDEN response), and so the
                # operation must not be something that the current
                # user is authorised to do.  They need to try as root,
                # and supply the password.  However, to get the right
                # prompt, we need to try as root but with no password
                # first.
                debugprint ("Authentication: Try as root")
                self._use_user = 'root'
                self._auth_called = False
                self._connect ()
                return 1

        if not self._prompt_allowed:
            debugprint ("Authentication: prompting not allowed")
            self._cancel = True
            return 1

        if not self._auth_called:
            # We aren't even getting a chance to supply credentials.
            debugprint ("Authentication: giving up")
            self._cancel = True
            self._cannot_auth = True
            return 1

        # Reset the flag indicating whether we were given an auth callback.
        self._auth_called = False

        # If we're previously prompted, explain why we're prompting again.
        if self._dialog_shown:
            if self._lock:
                self._gui_event.clear ()
                gobject.timeout_add (1, self._show_not_authorized_dialog)
                self._gui_event.wait ()
            else:
                self._show_not_authorized_dialog ()

        if self._lock:
            self._gui_event.clear ()
            gobject.timeout_add (1, self._perform_authentication_with_dialog)
            self._gui_event.wait ()
        else:
            self._perform_authentication_with_dialog ()

        if self._cancel:
            debugprint ("cancelled")
            return -1

        cups.setUser (self._use_user)
        debugprint ("Authentication: Reconnect")
        self._connect ()
        return 1

    def _show_not_authorized_dialog (self):
        if self._lock:
            gtk.gdk.threads_enter ()
        d = gtk.MessageDialog (self._parent,
                               gtk.DIALOG_MODAL |
                               gtk.DIALOG_DESTROY_WITH_PARENT,
                               gtk.MESSAGE_ERROR,
                               gtk.BUTTONS_CLOSE)
        d.set_title (_("Not authorized"))
        d.set_markup ('<span weight="bold" size="larger">' +
                      _("Not authorized") + '</span>\n\n' +
                      _("The password may be incorrect."))
        if self._lock:
            d.connect ("response", self._on_not_authorized_dialog_response)
            gtk.gdk.threads_leave ()
        else:
            d.run ()
            d.destroy ()

    def _on_not_authorized_dialog_response (self, dialog, response):
        self._gui_event.set ()
        dialog.destroy ()

    def _perform_authentication_with_dialog (self):
        if self._lock:
            gtk.gdk.threads_enter ()

        # Prompt.
        if len (self._operation_stack) > 0:
            d = AuthDialog (title=_("Authentication (%s)") %
                            self._operation_stack[0],
                            parent=self._parent)
        else:
            d = AuthDialog (parent=self._parent)

        d.set_prompt (self._prompt)
        d.set_auth_info ([self._use_user, ''])
        d.field_grab_focus ('password')
        d.set_keep_above (True)
        d.show_all ()
        d.show_now ()
        gtk.gdk.keyboard_grab (d.window, True)
        gtk.gdk.pointer_grab (d.window, True)
        self._dialog_shown = True
        if self._lock:
            d.connect ("response", self._on_authentication_response)
            gtk.gdk.threads_leave ()
        else:
            response = d.run ()
            self._on_authentication_response (d, response)

    def _on_authentication_response (self, dialog, response):
        gtk.gdk.pointer_ungrab ()
        gtk.gdk.keyboard_ungrab ()
        (self._use_user,
         self._use_password) = dialog.get_auth_info ()
        dialog.destroy ()

        if (response == gtk.RESPONSE_CANCEL or
            response == gtk.RESPONSE_DELETE_EVENT):
            self._cancel = True

        if self._lock:
            self._gui_event.set ()

if __name__ == '__main__':
    # Test it out.
    gtk.gdk.threads_init ()
    from timedops import TimedOperation
    set_debugging (True)
    c = TimedOperation (Connection, args=(None,)).run ()
    debugprint ("Connected")
    c._set_lock (True)
    print TimedOperation (c.getFile,
                          args=('/admin/conf/cupsd.conf',
                                '/dev/stdout')).run ()
