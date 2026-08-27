// LSD Window Geometry
//
// A Wayland client can neither position its window nor learn where it is;
// only the compositor knows. This extension runs inside GNOME Shell and
// publishes what the studio needs over the session bus:
//
//   org.latentdescent.WindowGeometry  at  /org/latentdescent/WindowGeometry
//     GetWindows(pid)  -> the frame (xdg window geometry) and buffer (whole
//                         surface) rects of every window owned by ``pid``
//                         (0 = all windows), in logical screen pixels
//     GetMonitors()    -> every monitor's geometry, work area and scale
//     Watch(pid) / Unwatch(pid)
//     signal Geometry(window)   -> a watched window moved or resized
//     signal Removed(id, pid)   -> a watched window closed
//     signal MonitorsChanged()  -> monitors / work areas changed
//     property Version          -> protocol version (bump on any change)
//
// The studio side lives in src/lsd/gl_gui/installation_helper.py (install /
// uninstall / status) and reads the feed for its OS-edge physics.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const BUS_NAME = 'org.latentdescent.WindowGeometry';
const OBJECT_PATH = '/org/latentdescent/WindowGeometry';
// Bump whenever the interface or the dict keys change; the studio compares
// it against the extension files it ships to offer a reinstall.
const PROTOCOL_VERSION = 1;

const IFACE_XML = `
<node>
  <interface name="org.latentdescent.WindowGeometry">
    <method name="GetWindows">
      <arg type="i" name="pid" direction="in"/>
      <arg type="aa{sv}" name="windows" direction="out"/>
    </method>
    <method name="GetMonitors">
      <arg type="aa{sv}" name="monitors" direction="out"/>
    </method>
    <method name="Watch">
      <arg type="i" name="pid" direction="in"/>
    </method>
    <method name="Unwatch">
      <arg type="i" name="pid" direction="in"/>
    </method>
    <signal name="Geometry">
      <arg type="a{sv}" name="window"/>
    </signal>
    <signal name="Removed">
      <arg type="t" name="id"/>
      <arg type="i" name="pid"/>
    </signal>
    <signal name="MonitorsChanged"/>
    <property name="Version" type="u" access="read"/>
  </interface>
</node>`;

function windowInfo(win) {
    // frame = the xdg window geometry (what the compositor places and
    // constrains — the studio sets it to its content rect); buffer = the
    // whole surface including any transparent shadow margin.
    const frame = win.get_frame_rect();
    const buffer = win.get_buffer_rect();
    const v = (type, value) => new GLib.Variant(type, value);
    return {
        id: v('t', win.get_id()),
        pid: v('i', win.get_pid()),
        wm_class: v('s', win.get_wm_class() ?? ''),
        title: v('s', win.get_title() ?? ''),
        x: v('i', frame.x),
        y: v('i', frame.y),
        width: v('i', frame.width),
        height: v('i', frame.height),
        buffer_x: v('i', buffer.x),
        buffer_y: v('i', buffer.y),
        buffer_width: v('i', buffer.width),
        buffer_height: v('i', buffer.height),
        monitor: v('i', win.get_monitor()),
        maximized: v('b', win.get_maximized() !== 0),
        fullscreen: v('b', win.is_fullscreen()),
        focused: v('b', win.has_focus()),
    };
}

function monitorInfo(index) {
    const display = global.display;
    const geometry = display.get_monitor_geometry(index);
    const workspace = global.workspace_manager.get_active_workspace();
    const work = workspace.get_work_area_for_monitor(index);
    const v = (type, value) => new GLib.Variant(type, value);
    return {
        index: v('i', index),
        x: v('i', geometry.x),
        y: v('i', geometry.y),
        width: v('i', geometry.width),
        height: v('i', geometry.height),
        work_x: v('i', work.x),
        work_y: v('i', work.y),
        work_width: v('i', work.width),
        work_height: v('i', work.height),
        scale: v('d', display.get_monitor_scale(index)),
        primary: v('b', index === display.get_primary_monitor()),
    };
}

export default class LsdWindowGeometry extends Extension {
    enable() {
        this._watched = new Set();          // pids whose windows emit Geometry
        this._handlers = new Map();         // MetaWindow -> [signal ids]

        this._dbus = Gio.DBusExportedObject.wrapJSObject(IFACE_XML, this);
        this._dbus.export(Gio.DBus.session, OBJECT_PATH);
        this._nameId = Gio.bus_own_name(
            Gio.BusType.SESSION, BUS_NAME,
            Gio.BusNameOwnerFlags.ALLOW_REPLACEMENT | Gio.BusNameOwnerFlags.REPLACE,
            null, null, null);

        this._createdId = global.display.connect(
            'window-created', (_display, win) => this._track(win));
        this._workareasId = global.display.connect(
            'workareas-changed', () => this._emitMonitorsChanged());
        this._monitorsId = Main.layoutManager.connect(
            'monitors-changed', () => this._emitMonitorsChanged());

        // Windows already open when the extension is enabled never emit
        // 'window-created' for us — attach to them directly.
        for (const actor of global.get_window_actors())
            this._track(actor.meta_window);
    }

    disable() {
        if (this._createdId) {
            global.display.disconnect(this._createdId);
            this._createdId = 0;
        }
        if (this._workareasId) {
            global.display.disconnect(this._workareasId);
            this._workareasId = 0;
        }
        if (this._monitorsId) {
            Main.layoutManager.disconnect(this._monitorsId);
            this._monitorsId = 0;
        }
        for (const [win, ids] of this._handlers) {
            for (const id of ids)
                win.disconnect(id);
        }
        this._handlers.clear();
        this._watched.clear();
        if (this._nameId) {
            Gio.bus_unown_name(this._nameId);
            this._nameId = 0;
        }
        if (this._dbus) {
            this._dbus.unexport();
            this._dbus = null;
        }
    }

    // ---- D-Bus interface -------------------------------------------------

    get Version() {
        return PROTOCOL_VERSION;
    }

    GetWindows(pid) {
        const out = [];
        for (const actor of global.get_window_actors()) {
            const win = actor.meta_window;
            if (!win)
                continue;
            if (pid !== 0 && win.get_pid() !== pid)
                continue;
            out.push(windowInfo(win));
        }
        return out;
    }

    GetMonitors() {
        const out = [];
        const count = global.display.get_n_monitors();
        for (let index = 0; index < count; index++)
            out.push(monitorInfo(index));
        return out;
    }

    Watch(pid) {
        this._watched.add(pid);
    }

    Unwatch(pid) {
        this._watched.delete(pid);
    }

    // ---- window tracking -------------------------------------------------

    _track(win) {
        if (!win || this._handlers.has(win))
            return;
        // Every window type: the studio's own windows are NORMAL, but the
        // feed is generic (a popup of ours could be asked for too).
        const changed = () => this._onChanged(win);
        const ids = [
            win.connect('position-changed', changed),
            win.connect('size-changed', changed),
            win.connect('unmanaged', () => this._untrack(win)),
        ];
        this._handlers.set(win, ids);
    }

    _untrack(win) {
        const ids = this._handlers.get(win);
        if (ids) {
            for (const id of ids)
                win.disconnect(id);
            this._handlers.delete(win);
        }
        if (this._dbus && this._watched.has(win.get_pid())) {
            this._dbus.emit_signal('Removed',
                new GLib.Variant('(ti)', [win.get_id(), win.get_pid()]));
        }
    }

    _onChanged(win) {
        if (!this._dbus || !this._watched.has(win.get_pid()))
            return;
        this._dbus.emit_signal('Geometry',
            new GLib.Variant('(a{sv})', [windowInfo(win)]));
    }

    _emitMonitorsChanged() {
        if (this._dbus)
            this._dbus.emit_signal('MonitorsChanged', null);
    }
}
