# SPDX-License-Identifier: MPL-2.0
"""A searchable, recycling local design library built from shared Luma parts."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
import os
from pathlib import Path
import uuid
from gi.repository import Adw, Gdk, Gio, GLib, Gtk
from luma_appkit import FileCard, MediaGrid, MediaItem, BarAction, PropertyChoice, BarSearch, ActionCenter, BarWidget
from luma_appkit.action_center import make_control
from luma_appkit.action_bubble import MenuItem
from luma_appkit.menus import bar_menu
from .library_store import LibraryOrganization, saved_documents
from .persistence import DocumentStore


class LibraryChoice(Gtk.Box):
    """Collection choices use the same Luma field as the creative inspector."""
    def __init__(self, label, names, changed, *, segments=False, icons=()):
        super().__init__()
        self.label, self.names, self.changed, self.selected = label, list(names), changed, 0
        self.segments, self.icons = segments, icons
        self._rebuild()

    def _rebuild(self):
        if (child := self.get_first_child()) is not None:
            self.remove(child)
        self.field = PropertyChoice(self.label, [(str(i), name, self.icons[i] if i < len(self.icons) else None) for i, name in enumerate(self.names)],
            segments=self.segments, current=str(self.selected), on_change=lambda key: self.set_selected(int(key)))
        self.append(self.field)

    def get_selected(self):
        return self.selected

    def set_selected(self, selected):
        self.selected = selected
        self.field.set_current(str(selected))
        self.changed()

    def set_model(self, model):
        self.names = [model.get_string(i) for i in range(model.get_n_items())]
        self.selected = min(self.selected, max(0, len(self.names) - 1))
        self._rebuild()


class DocumentLibrary(Gtk.Box):
    def __init__(self, host):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.host = host
        self.organization = LibraryOrganization()
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='canvas-library')
        self._generation = 0
        self._requested = set()
        self._pending = set()
        self.connect("unmap", lambda *_: [future.cancel() for future in tuple(self._pending)])
        self._scope = 'all'
        self._recent = set()
        self._ready = False
        for side in ('start', 'end', 'top', 'bottom'):
            getattr(self, f'set_margin_{side}')(16)
        header = Gtk.Box(spacing=12)
        title = Gtk.Label(label='Your designs', xalign=0, hexpand=True)
        title.add_css_class('title-2')
        header.append(title)
        self.append(header)
        explanation = Gtk.Label(label='Saved automatically. Open a design to pick up where you left off.',
                                xalign=0, wrap=True, natural_wrap_mode=Gtk.NaturalWrapMode.NONE)
        explanation.add_css_class('dim-label')
        self.append(explanation)
        self.scope = LibraryChoice('Collection', ['All designs', 'Recent', 'Starred'], self._scope_changed)
        self.scope.set_tooltip_text('Design collection')
        self.sort = LibraryChoice('Sort', ['Last modified', 'Name A–Z'],
            lambda: self.sorter.changed(Gtk.SorterChange.DIFFERENT))
        self.view_choice = LibraryChoice('View', ['Grid', 'List'], lambda: self.views.set_visible_child_name(
            'grid' if self.view_choice.get_selected() == 0 else 'list'), segments=True, icons=('grid-2x2', 'list'))
        self.search_item = BarSearch('Search designs', label='Search saved Canvas designs', span='wide',
            on_change=lambda _text: self._filter())
        self.count = Gtk.Label(label='Loading designs…', xalign=0)
        self.count.add_css_class('dim-label')
        self.append(self.count)
        self.store = Gio.ListStore(item_type=MediaItem)
        self.filter = Gtk.CustomFilter.new(self._matches)
        self.filtered = Gtk.FilterListModel(model=self.store, filter=self.filter)
        self.sorter = Gtk.CustomSorter.new(self._compare)
        self.model = Gtk.SortListModel(model=self.filtered, sorter=self.sorter)
        self.model.connect('items-changed', lambda *_: self._update_empty())
        self.grid = MediaGrid(self.model, kind='library', min_side=220, label='Saved Canvas designs',
                              on_open=lambda item: host._open_saved_design(item.data.path),
                              meta=self._tile_meta)
        self.grid.set_vexpand(True)
        factory = Gtk.SignalListItemFactory()
        factory.connect('setup', lambda _factory, row: row.set_child(Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=4)))
        factory.connect('bind', self._bind_list)
        factory.connect('unbind', lambda _factory, row: self._clear(row.get_child()))
        self.list = Gtk.ListView(model=Gtk.NoSelection(model=self.model), factory=factory)
        self.list.connect('activate', lambda _list, index: host._open_saved_design(self.model.get_item(index).data.path))
        self.views = Gtk.Stack(vexpand=True, hhomogeneous=False, vhomogeneous=False)
        self.views.add_named(self.grid, 'grid')
        self.views.add_named(Gtk.ScrolledWindow(child=self.list, hscrollbar_policy=Gtk.PolicyType.NEVER), 'list')
        library_body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, vexpand=True)
        library_body.append(self.views)
        self.empty = Adw.StatusPage(title='No saved designs yet',
                                   description='Start a new design, or browse for a Canvas file.',
                                   icon_name='folder-open-symbolic', vexpand=True)
        library_body.append(self.empty)
        from luma_appkit.structure_layers import LayerHost
        self.library_host = LayerHost(library_body, name='canvas-library')
        self.library_host.set_vexpand(True)
        self.append(self.library_host)
        self.actions = ActionCenter().attach(self.library_host)
        self._desktop_items = [
            BarAction('plus', 'New design', host._new, primary=True),
            BarAction('folder-open', 'Browse files…', host._browse_files),
            BarWidget(self.scope),
            BarAction('folder-plus', 'New project', lambda: self._new_project(None)),
            self.search_item, BarWidget(self.sort), BarWidget(self.view_choice),
        ]
        self.actions.show_bar(self._desktop_items)
        self.search = self.search_item.entry
        self.actions.attach_scroller(self.list.get_parent())
        if self.grid.view is not None:self.actions.attach_scroller(self.grid.view.get_parent())
        self.add_tick_callback(self._viewport_room)
        self._refresh_scopes()
        self._phone = None
        from luma_appkit.structure_adapt import WidthWatch
        self._width_watch = WidthWatch(self, self._responsive, threshold=599)

    def _viewport_room(self, _widget, _clock):
        # Keep the actual list/grid viewport above the floating phone controls;
        # content-padding alone allows tiles to pass behind the bar mid-scroll.
        found,bounds=self.actions.bar.compute_bounds(self.library_host)
        room=max(0,int(self.library_host.get_height()-bounds.get_y())+16) if self._phone and found else 0
        for child in (self.views,self.empty):
            if child.get_margin_bottom()!=room:child.set_margin_bottom(room)
        return True

    def _phone_options(self,options):
        from luma_appkit.bar_panel import panel_list
        from luma_appkit.action_center import make_control
        import copy
        if self.actions.grown=='library-options':self.actions.fold();return
        rows=[]
        def choose(callback):
            self.actions.fold()
            if callback:callback()
        for item in options():
            row=copy.copy(item);row.on_activate=lambda callback=item.on_activate:choose(callback);rows.append(row)
        body=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=8)
        header=Gtk.Box(spacing=8)
        label=Gtk.Label(label='Collections and view',xalign=0,hexpand=True);label.add_css_class('heading')
        header.append(label);header.append(make_control(BarAction('x',tooltip='Close options',on_activate=self.actions.fold)))
        body.append(header)
        scroll=Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,propagate_natural_height=True,
            max_content_height=max(96,min(280,int(self.get_height()*.32))),child=panel_list(rows,label='Collections and view'))
        body.append(scroll);self._options_scroller=scroll
        self.actions.grow('library-options',body)

    def _responsive(self, width):
        phone = 0 < width < 600
        if phone == self._phone:
            return
        self._phone = phone
        self.actions.hide_bar()
        for widget in (self.scope, self.sort, self.view_choice):
            parent = widget.get_parent()
            if parent is not None:
                parent.remove(widget)
        if phone:
            def options():
                return ([MenuItem(name, on_activate=lambda i=i:self.scope.set_selected(i))
                         for i, (_,name) in enumerate(self._scopes)] +
                        [MenuItem('New project',icon='folder-plus',on_activate=lambda:self._new_project(None)),
                         MenuItem('Last modified',on_activate=lambda:self.sort.set_selected(0)),
                         MenuItem('Name A–Z',on_activate=lambda:self.sort.set_selected(1)),
                         MenuItem('Grid',icon='grid-2x2',on_activate=lambda:self.view_choice.set_selected(0)),
                         MenuItem('List',icon='list',on_activate=lambda:self.view_choice.set_selected(1))])
            self.actions.show_bar([
                BarAction('plus','New design',self.host._new,primary=True),
                BarAction('folder-open',tooltip='Browse files',on_activate=self.host._browse_files),
                BarAction('ellipsis',tooltip='Collections and view',on_activate=lambda:self._phone_options(options)),
            ],entry=self.search_item,fill=True)
        else:
            self.actions.show_bar(self._desktop_items)
        self.search = self.search_item.entry

    @staticmethod
    def _clear(box):
        while (child := box.get_first_child()) is not None:
            box.remove(child)

    def _refresh_scopes(self):
        self._scopes = [('all', 'All designs'), ('recent', 'Recent'), ('starred', 'Starred')]
        self._scopes += [(key, name) for key, name in self.organization.projects.items()]
        current = next((i for i, (key, _) in enumerate(self._scopes) if key == self._scope), 0)
        self.scope.set_model(Gtk.StringList.new([name for _, name in self._scopes]))
        self.scope.set_selected(current)

    def _scope_changed(self, *_):
        if not hasattr(self, '_scopes'):
            return
        index = self.scope.get_selected()
        if index < len(self._scopes):
            self._scope = self._scopes[index][0]
            if hasattr(self, 'filter'):
                self._filter()

    def _matches(self, item, *_):
        path = str(item.data.path)
        query = self.search.get_text().strip().casefold()
        if query not in item.title.casefold():
            return False
        if self._scope == 'recent':
            return path in self._recent
        if self._scope == 'starred':
            return path in self.organization.starred
        if self._scope != 'all':
            return self.organization.assignments.get(path) == self._scope
        return True

    def _compare(self, left, right, *_):
        a, b = (left.title.casefold(), str(left.data.path)), (right.title.casefold(), str(right.data.path))
        if self.sort.get_selected() == 0:
            a, b = (-left.data.modified, a), (-right.data.modified, b)
        return Gtk.Ordering.SMALLER if a < b else Gtk.Ordering.LARGER if a > b else Gtk.Ordering.EQUAL

    def _filter(self):
        self.filter.changed(Gtk.FilterChange.DIFFERENT)
        self._update_empty()

    def _update_empty(self):
        if not hasattr(self, 'empty'):
            return
        count = self.model.get_n_items()
        self.count.set_label(f'{count:,} design' + ('' if count == 1 else 's'))
        self.views.set_visible(count > 0)
        self.empty.set_visible(count == 0 and self._ready)
        self.empty.set_title('No matching designs' if self.store.get_n_items() else 'No saved designs yet')
        self.empty.set_description('Try another search or collection.' if self.store.get_n_items() else
                                   'Start a new design, or browse for a Canvas file.')

    def refresh(self):
        self._generation += 1
        generation = self._generation
        self._ready = False
        self.count.set_label('Loading designs…')
        recent = self.host.get_application().recent_documents()
        future = self.pool.submit(saved_documents, recent)
        def ready():
            if generation != self._generation:
                return False
            try:
                designs = future.result()
            except OSError as error:
                self.count.set_label(f'Could not read saved designs: {error}')
                return False
            self._recent = {str(file.path) for file in designs[:50]}
            self._requested.clear()
            items = [MediaItem(str(file.path), title=file.path.stem, caption=file.path.stem,
                               favourite=str(file.path) in self.organization.starred, data=file) for file in designs]
            self.store.splice(0, self.store.get_n_items(), items)
            self._ready = True
            self._update_empty()
            return False
        future.add_done_callback(lambda _future: GLib.idle_add(ready))

    def _tile_meta(self, item):
        self._thumbnail(item)
        line = Gtk.Box(spacing=8)
        date = datetime.fromtimestamp(item.data.modified).strftime('%b %d, %Y')
        line.append(Gtk.Label(label=date, xalign=0, hexpand=True))
        more = make_control(BarAction('ellipsis', None, lambda: self._organize(more, item),
            tooltip=f'Organize {item.title}'), size='tool')
        line.append(more)
        return line

    def _bind_list(self, _factory, row):
        item = row.get_item()
        box = row.get_child()
        self._clear(box)
        card = FileCard(str(item.data.path), name=item.data.path.name, kind='Canvas design', compact=True,
                        subtitle=datetime.fromtimestamp(item.data.modified).strftime('Saved %b %d, %Y'),
                        on_open=lambda: self.host._open_saved_design(item.data.path))
        box.append(card)
        box.append(self._tile_meta(item))

    def _thumbnail(self, item):
        # Read/render only a bound tile, on two workers; cache by exact file stat.
        key = hashlib.sha256(f'{item.id}:{item.data.modified}:{item.data.size}'.encode()).hexdigest()
        if key in self._requested:
            return
        self._requested.add(key)
        cache = Path(os.environ.get('XDG_CACHE_HOME', str(Path.home() / '.cache'))) / 'canvas-library'
        target = cache / (key + '.png')
        def render():
            if not target.exists():
                from .export import thumbnail_png
                data = thumbnail_png(DocumentStore.load(item.data.path), 320)
                cache.mkdir(parents=True, exist_ok=True)
                temporary = target.with_suffix(f'.{uuid.uuid4()}.tmp')
                temporary.write_bytes(data)
                temporary.replace(target)
            return target
        future = self.pool.submit(render)
        self._pending.add(future)
        def ready():
            self._pending.discard(future)
            try:
                item.picture = str(future.result())
            except Exception:
                # Damaged files stay visible and opening reports the real error.
                pass
            return False
        future.add_done_callback(lambda _future: GLib.idle_add(ready))

    def _persist(self):
        try:
            self.organization.save()
        except OSError as error:
            self.host._toast(f'Could not save library organization: {error}', warning=True)
            return False
        self._filter()
        return True

    def _organize(self, anchor, item):
        path = item.id
        def star():
            if path in self.organization.starred:
                self.organization.starred.remove(path)
            else:
                self.organization.starred.add(path)
            item.favourite = path in self.organization.starred
            self._persist()
        rows = [MenuItem('Unstar' if item.favourite else 'Star', icon='star', on_activate=star)]
        rows.append(MenuItem('All designs', icon='layers', on_activate=lambda: self._assign(item, None)))
        rows += [MenuItem(f'Project: {name}', icon='folder',
                          on_activate=lambda key=key: self._assign(item, key))
                 for key, name in self.organization.projects.items()]
        bar_menu(anchor, rows, label=f'Organize {item.title}')

    def _assign(self, item, project):
        if project is None:
            self.organization.assignments.pop(item.id, None)
        else:
            self.organization.assignments[item.id] = project
        self._persist()

    def _new_project(self, *_):
        dialog = Adw.AlertDialog(heading='New project', body='Group related designs in your library.')
        entry = Gtk.Entry(placeholder_text='Project name', activates_default=True)
        dialog.set_extra_child(entry)
        dialog.add_response('cancel', 'Cancel')
        dialog.add_response('create', 'Create project')
        dialog.set_response_appearance('create', Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response('create')
        dialog.set_close_response('cancel')
        dialog.set_response_enabled('create', False)
        entry.connect('changed', lambda *_: dialog.set_response_enabled('create', bool(entry.get_text().strip())))
        def answered(_dialog, response):
            if response == 'create':
                key = str(uuid.uuid4())
                self.organization.projects[key] = entry.get_text().strip()
                if self._persist():
                    self._scope = key
                    self._refresh_scopes()
                    self._filter()
        dialog.connect('response', answered)
        dialog.present(self.host)
        entry.grab_focus()
