/* SPDX-License-Identifier: Apache-2.0 */
/* LumaDetailsPane and its rows: the twin of structure_details.py. */
#include "luma-ui.h"
#include "luma-details-pane.h"
#include "luma-layer-host.h"

static gboolean stop(gpointer data) {
  *(gboolean *)data = TRUE;
  return G_SOURCE_REMOVE;
}

static void settle(void) {
  gboolean done = FALSE;
  g_timeout_add(300, stop, &done);
  while (!done)
    g_main_context_iteration(NULL, TRUE);
}

static int count(GtkWidget *box) {
  int n = 0;
  for (GtkWidget *c = gtk_widget_get_first_child(box); c != NULL; c = gtk_widget_get_next_sibling(c))
    n++;
  return n;
}

static GtkWidget *sheet_of(GtkWidget *pane) { return gtk_widget_get_first_child(pane); }

static GtkWidget *body_of(GtkWidget *sheet) {
  GtkWidget *scroller = gtk_widget_get_last_child(sheet);
  return gtk_viewport_get_child(GTK_VIEWPORT(gtk_scrolled_window_get_child(GTK_SCROLLED_WINDOW(scroller))));
}

static void notified(GObject *object G_GNUC_UNUSED, GParamSpec *pspec G_GNUC_UNUSED, gpointer data) {
  (*(int *)data)++;
}

static void counted(GObject *object G_GNUC_UNUSED, gpointer data) { (*(int *)data)++; }

static void photo(GObject *object G_GNUC_UNUSED, guint index, gpointer data) { *(guint *)data = index; }

static void test_rows(void) {
  GtkWidget *row = g_object_ref_sink(luma_details_row_new("Priya Raman", "@priya", NULL));
  g_assert_true(gtk_widget_has_css_class(row, "lumaui-details-row"));
  GtkWidget *actions = gtk_widget_get_last_child(row);
  g_assert_false(gtk_widget_get_visible(actions));
  luma_details_row_add_action(LUMA_DETAILS_ROW(row), "phone", "Call", "win.call");
  g_assert_true(gtk_widget_get_visible(actions));
  g_assert_true(gtk_widget_has_css_class(gtk_widget_get_first_child(actions), "lumaui-details-action"));
  int activated = 0;
  g_signal_connect(row, "activated", G_CALLBACK(counted), &activated);
  g_signal_emit_by_name(gtk_widget_get_first_child(row), "clicked");
  g_assert_cmpint(activated, ==, 1);
  g_object_unref(row);

  GtkWidget *item = g_object_ref_sink(luma_details_item_new("Standup", "Every weekday", "calendar"));
  g_assert_true(GTK_IS_BUTTON(item));
  GtkWidget *line = gtk_button_get_child(GTK_BUTTON(item));
  g_assert_true(gtk_widget_has_css_class(gtk_widget_get_first_child(line), "lumaui-details-item-icon"));
  GtkWidget *lead = gtk_label_new("x");
  luma_details_item_set_lead(LUMA_DETAILS_ITEM(item), lead);
  g_assert_true(gtk_widget_get_first_child(line) == lead);
  g_assert_cmpint(count(line), ==, 2);
  GtkWidget *trail = gtk_label_new("9:30");
  luma_details_item_set_trail(LUMA_DETAILS_ITEM(item), trail);
  g_assert_true(gtk_widget_get_last_child(line) == trail);
  luma_details_item_set_trail(LUMA_DETAILS_ITEM(item), NULL);
  g_assert_cmpint(count(line), ==, 2);
  luma_details_item_set_selected(LUMA_DETAILS_ITEM(item), TRUE);
  g_assert_true(gtk_widget_has_css_class(item, "on"));
  g_object_unref(item);

  GtkWidget *add = g_object_ref_sink(luma_add_row_new("Add people", NULL, "<Control>n"));
  g_assert_true(gtk_widget_has_css_class(add, "lumaui-add-row"));
  line = gtk_button_get_child(GTK_BUTTON(add));
  g_assert_true(gtk_widget_has_css_class(gtk_widget_get_last_child(line), "lumaui-add-shortcut"));
  GtkWidget *add_label = gtk_widget_get_next_sibling(gtk_widget_get_first_child(line));
  luma_add_row_set_document(LUMA_ADD_ROW(add), TRUE);
  g_assert_cmpint(gtk_label_get_max_width_chars(GTK_LABEL(add_label)), ==, -1);
  luma_add_row_set_document(LUMA_ADD_ROW(add), FALSE);
  g_assert_cmpint(gtk_label_get_max_width_chars(GTK_LABEL(add_label)), ==, 1);
  g_object_unref(add);

  GtkWidget *fact = g_object_ref_sink(luma_fact_row_new("phone", "Mobile", "+44 7700 900123", TRUE));
  GtkWidget *copy = gtk_widget_get_last_child(fact);
  g_assert_true(gtk_widget_has_css_class(copy, "lumaui-fact-copy"));
  g_assert_cmpstr(gtk_widget_get_tooltip_text(copy), ==, "Copy mobile");
  luma_fact_row_copy(LUMA_FACT_ROW(fact)); /* no window: the clipboard only, no toast */
  g_object_unref(fact);
}

static void test_photos(void) {
  g_autoptr(GListStore) store = g_list_store_new(G_TYPE_OBJECT);
  for (int i = 0; i < 5; i++) {
    g_autoptr(GdkPaintable) paintable = gdk_paintable_new_empty(40, 40);
    g_list_store_append(store, paintable);
  }
  GtkWidget *photos = g_object_ref_sink(luma_details_photos_new(G_LIST_MODEL(store)));
  g_assert_cmpint(count(photos), ==, 5);
  GtkWidget *fifth = gtk_grid_get_child_at(GTK_GRID(photos), 1, 1);
  g_assert_nonnull(fifth);
  g_assert_true(gtk_widget_has_css_class(fifth, "lumaui-details-photo"));
  guint index = 0;
  g_signal_connect(photos, "photo-activated", G_CALLBACK(photo), &index);
  g_signal_emit_by_name(photos, "photo-activated", 4);
  g_assert_cmpuint(index, ==, 4);
  g_autoptr(GListStore) wrong = g_list_store_new(G_TYPE_OBJECT);
  g_autoptr(GObject) thing = g_object_new(G_TYPE_OBJECT, NULL);
  g_list_store_append(wrong, thing);
  g_test_expect_message(G_LOG_DOMAIN, G_LOG_LEVEL_CRITICAL, "*GdkPaintable or a GFile*");
  luma_details_photos_set_items(LUMA_DETAILS_PHOTOS(photos), G_LIST_MODEL(wrong));
  g_test_assert_expected_messages();
  g_assert_cmpint(count(photos), ==, 0);
  g_object_unref(photos);
}

static void test_content(void) {
  GtkWidget *pane = g_object_ref_sink(luma_details_pane_new(NULL, FALSE));
  GtkWidget *sheet = sheet_of(pane);
  GtkWidget *header = gtk_widget_get_next_sibling(gtk_widget_get_first_child(sheet));
  GtkWidget *back = gtk_widget_get_first_child(header);
  GtkWidget *title = gtk_widget_get_next_sibling(back);
  GtkWidget *leading = gtk_image_new_from_icon_name("document-open-symbolic");
  luma_details_pane_set_leading(LUMA_DETAILS_PANE(pane), leading);
  g_object_ref(leading);
  g_assert_true(gtk_widget_get_next_sibling(back) == leading);
  g_assert_true(gtk_widget_get_next_sibling(leading) == title);
  g_assert_true(gtk_widget_has_css_class(leading, "lumaui-details-leading"));
  luma_details_pane_set_leading(LUMA_DETAILS_PANE(pane), leading); /* no-op */
  g_assert_true(gtk_widget_get_next_sibling(back) == leading);
  GtkWidget *replacement = gtk_label_new("T");
  luma_details_pane_set_leading(LUMA_DETAILS_PANE(pane), replacement);
  g_object_ref(replacement);
  g_assert_null(gtk_widget_get_parent(leading));
  g_assert_true(gtk_widget_get_next_sibling(back) == replacement);
  luma_details_pane_set_leading(LUMA_DETAILS_PANE(pane), NULL);
  g_assert_null(gtk_widget_get_parent(replacement));
  g_assert_true(gtk_widget_get_next_sibling(back) == title);
  GtkWidget *entry = gtk_entry_new();
  luma_details_pane_set_title_content(LUMA_DETAILS_PANE(pane), entry);
  g_object_ref(entry);
  g_assert_false(gtk_widget_get_visible(title));
  g_assert_true(gtk_widget_get_next_sibling(title) == entry);
  g_assert_true(gtk_widget_get_next_sibling(entry) == gtk_widget_get_last_child(header));
  g_assert_true(gtk_widget_get_hexpand(entry));
  luma_details_pane_set_title_content(LUMA_DETAILS_PANE(pane), entry);
  luma_details_pane_set_title_content(LUMA_DETAILS_PANE(pane), NULL);
  g_assert_true(gtk_widget_get_visible(title));
  g_assert_null(gtk_widget_get_parent(entry));
  g_object_unref(entry);
  g_object_unref(leading);
  g_object_unref(replacement);
  g_assert_true(gtk_widget_has_css_class(pane, "lumaui-details-slot"));
  g_assert_true(gtk_widget_has_css_class(sheet, "lumaui-details"));
  g_assert_false(gtk_widget_get_visible(sheet));
  GtkWidget *body = body_of(sheet);
  luma_details_pane_add_hero(LUMA_DETAILS_PANE(pane), "Launch crew", "4 people", NULL);
  luma_details_pane_add_fact(LUMA_DETAILS_PANE(pane), "Created", "Sep 2");
  luma_details_pane_add_fact(LUMA_DETAILS_PANE(pane), "Encryption", "End-to-end");
  luma_details_pane_add_section(LUMA_DETAILS_PANE(pane), "People", "View all", "win.people");
  luma_details_pane_add_row(LUMA_DETAILS_PANE(pane), luma_details_row_new("Priya", NULL, NULL));
  luma_details_pane_add_row(LUMA_DETAILS_PANE(pane), luma_add_row_new("Add people", NULL, NULL));
  luma_details_pane_add_fact(LUMA_DETAILS_PANE(pane), "Size", "2 MB");
  g_assert_cmpint(count(body), ==, 5); /* hero, facts, section, list, facts */
  GtkWidget *facts = gtk_widget_get_next_sibling(gtk_widget_get_first_child(body));
  g_assert_true(gtk_widget_has_css_class(facts, "lumaui-details-facts"));
  g_assert_cmpint(count(facts), ==, 2);
  GtkWidget *list = gtk_widget_get_prev_sibling(gtk_widget_get_last_child(body));
  g_assert_true(gtk_widget_has_css_class(list, "lumaui-details-list"));
  g_assert_cmpint(count(list), ==, 2);
  g_test_expect_message(G_LOG_DOMAIN, G_LOG_LEVEL_CRITICAL, "*details list holds*");
  luma_details_pane_add_row(LUMA_DETAILS_PANE(pane), gtk_label_new("nope"));
  g_test_assert_expected_messages();
  luma_details_pane_clear(LUMA_DETAILS_PANE(pane));
  g_assert_cmpint(count(body), ==, 0);
  g_object_unref(pane);
}

static void test_show(void) {
  GtkWidget *pane = g_object_ref_sink(luma_details_pane_new("Info", FALSE));
  int notifies = 0;
  g_signal_connect(pane, "notify::shown", G_CALLBACK(notified), &notifies);
  GtkWidget *info = g_object_ref_sink(luma_details_pane_info_button(LUMA_DETAILS_PANE(pane)));
  /* Nothing to describe: Info keeps it closed. */
  g_assert_false(luma_details_pane_show(LUMA_DETAILS_PANE(pane), TRUE));
  gtk_toggle_button_set_active(GTK_TOGGLE_BUTTON(info), TRUE);
  g_assert_false(gtk_toggle_button_get_active(GTK_TOGGLE_BUTTON(info)));
  g_autoptr(GObject) subject = g_object_new(G_TYPE_OBJECT, NULL);
  luma_details_pane_set_subject(LUMA_DETAILS_PANE(pane), subject);
  g_assert_false(luma_details_pane_get_shown(LUMA_DETAILS_PANE(pane)));
  gtk_toggle_button_set_active(GTK_TOGGLE_BUTTON(info), TRUE);
  g_assert_true(luma_details_pane_get_shown(LUMA_DETAILS_PANE(pane)));
  g_assert_true(gtk_widget_get_visible(sheet_of(pane)));
  g_assert_cmpint(notifies, ==, 1);
  /* The wish is kept while the subject changes. */
  g_autoptr(GObject) next = g_object_new(G_TYPE_OBJECT, NULL);
  luma_details_pane_set_subject(LUMA_DETAILS_PANE(pane), next);
  g_assert_true(luma_details_pane_get_shown(LUMA_DETAILS_PANE(pane)));
  g_assert_true(luma_details_pane_get_subject(LUMA_DETAILS_PANE(pane)) == next);
  /* The subject goes away: it closes and forgets. */
  luma_details_pane_set_subject(LUMA_DETAILS_PANE(pane), NULL);
  g_assert_false(luma_details_pane_get_shown(LUMA_DETAILS_PANE(pane)));
  g_assert_false(gtk_toggle_button_get_active(GTK_TOGGLE_BUTTON(info)));
  luma_details_pane_set_subject(LUMA_DETAILS_PANE(pane), subject);
  g_assert_false(luma_details_pane_get_shown(LUMA_DETAILS_PANE(pane)));
  g_assert_true(luma_details_pane_toggle(LUMA_DETAILS_PANE(pane)));
  luma_details_pane_close(LUMA_DETAILS_PANE(pane));
  g_assert_false(luma_details_pane_get_shown(LUMA_DETAILS_PANE(pane)));
  g_assert_cmpint(notifies, ==, 4);
  /* A main pane stays open, without a close button. */
  luma_details_pane_set_main(LUMA_DETAILS_PANE(pane), TRUE);
  g_assert_true(luma_details_pane_get_main(LUMA_DETAILS_PANE(pane)));
  g_assert_true(luma_details_pane_get_shown(LUMA_DETAILS_PANE(pane)));
  luma_details_pane_close(LUMA_DETAILS_PANE(pane));
  g_assert_true(luma_details_pane_get_shown(LUMA_DETAILS_PANE(pane)));
  g_object_unref(info);
  g_object_unref(pane);
}

static void test_drawer(void) {
  GtkWidget *window = gtk_window_new();
  gtk_window_set_default_size(GTK_WINDOW(window), 400, 700);
  GtkWidget *box = gtk_box_new(GTK_ORIENTATION_HORIZONTAL, 0);
  GtkWidget *pane = luma_details_pane_new("Details", FALSE);
  luma_details_pane_add_fact(LUMA_DETAILS_PANE(pane), "Size", "2 MB");
  gtk_box_append(GTK_BOX(box), gtk_label_new("island"));
  gtk_box_append(GTK_BOX(box), pane);
  gtk_window_set_child(GTK_WINDOW(window), box);
  gtk_window_present(GTK_WINDOW(window));
  settle();
  g_autoptr(GObject) subject = g_object_new(G_TYPE_OBJECT, NULL);
  luma_details_pane_set_subject(LUMA_DETAILS_PANE(pane), subject);
  g_assert_true(luma_details_pane_show(LUMA_DETAILS_PANE(pane), TRUE));
  g_assert_true(luma_details_pane_get_is_drawer(LUMA_DETAILS_PANE(pane)));
  g_assert_null(gtk_widget_get_first_child(pane));
  LumaLayerHost *host = luma_layer_host_window_host(pane);
  LumaModalHandle *modal = luma_layer_host_get_modal(host);
  g_assert_nonnull(modal);
  GtkWidget *sheet = luma_modal_handle_get_card(modal);
  g_assert_true(gtk_widget_has_css_class(sheet, "drawer"));
  /* Tapped outside: it closes and the sheet comes home. */
  int notifies = 0;
  g_signal_connect(pane, "notify::shown", G_CALLBACK(notified), &notifies);
  luma_modal_handle_cancel(modal);
  g_assert_false(luma_details_pane_get_shown(LUMA_DETAILS_PANE(pane)));
  g_assert_cmpint(notifies, ==, 1);
  settle();
  g_assert_true(gtk_widget_get_first_child(pane) == sheet);
  g_assert_false(gtk_widget_get_visible(sheet));
  g_assert_true(gtk_widget_has_css_class(sheet, "luma-island"));
  /* Open again, then widen: it goes back beside the island. */
  luma_details_pane_show(LUMA_DETAILS_PANE(pane), TRUE);
  g_assert_true(luma_details_pane_get_is_drawer(LUMA_DETAILS_PANE(pane)));
  gtk_window_set_default_size(GTK_WINDOW(window), 1000, 700);
  settle();
  g_assert_false(luma_details_pane_get_is_drawer(LUMA_DETAILS_PANE(pane)));
  g_assert_true(gtk_widget_get_first_child(pane) == sheet);
  g_assert_true(gtk_widget_get_visible(sheet));
  g_assert_false(gtk_widget_has_css_class(sheet, "drawer"));
  gtk_window_destroy(GTK_WINDOW(window));
}

static void test_embedded(void) {
  const int widths[] = {360, 402, 720, 1180};
  for (guint i = 0; i < G_N_ELEMENTS(widths); i++) {
    GtkWidget *window = gtk_window_new();
    gtk_widget_add_css_class(window, "luma-app-window");
    gtk_widget_add_css_class(window, "lumaui-phone-device");
    gtk_window_set_default_size(GTK_WINDOW(window), widths[i], 700);
    GtkWidget *host = luma_layer_host_new(gtk_label_new("Files"));
    gtk_window_set_child(GTK_WINDOW(window), host);
    LumaActionCenter *center = LUMA_ACTION_CENTER(luma_action_center_new(NULL));
    luma_action_center_attach(center, host);
    LumaBarItem *items[] = { luma_bar_item_new_action("copy", "Copy", NULL),
                            luma_bar_item_new_action("info", "Details", NULL),
                            luma_bar_item_new_action("x", "Done", NULL) };
    luma_action_center_show_bar(center, items, 3, NULL);
    GtkWidget *pane = luma_details_pane_new("Details", TRUE);
    luma_details_pane_set_embedded(LUMA_DETAILS_PANE(pane), TRUE);
    luma_details_pane_set_closable(LUMA_DETAILS_PANE(pane), FALSE);
    luma_details_pane_set_header_visible(LUMA_DETAILS_PANE(pane), FALSE);
    GtkWidget *thumbnail = gtk_image_new_from_icon_name("image-x-generic-symbolic");
    gtk_image_set_pixel_size(GTK_IMAGE(thumbnail), 44);
    luma_details_pane_add_subject(LUMA_DETAILS_PANE(pane),
        "A long selected photo name that must fit the panel.webp", "Image · 412 KB", thumbnail);
    luma_details_pane_add_fact(LUMA_DETAILS_PANE(pane), "Kind", "Text document");
    luma_details_pane_add_fact(LUMA_DETAILS_PANE(pane), "Size", "2.3 kB");
    gtk_window_present(GTK_WINDOW(window)); settle();
    luma_action_center_grow_panel(center, "details", pane); settle();
    g_assert_false(gtk_widget_has_css_class(sheet_of(pane), "luma-island"));
    g_assert_false(luma_details_pane_get_is_drawer(LUMA_DETAILS_PANE(pane)));
    g_assert_true(gtk_widget_is_ancestor(sheet_of(pane), GTK_WIDGET(center)));
    GtkWidget *copy = luma_action_center_get_bar_control(center, 0);
    g_assert_true(gtk_widget_get_mapped(copy));
    graphene_rect_t row, panel;
    g_assert_true(gtk_widget_compute_bounds(copy, window, &row));
    g_assert_true(gtk_widget_compute_bounds(pane, window, &panel));
    g_assert_cmpfloat(panel.size.height, <, 260);
    GtkWidget *subject = gtk_widget_get_first_child(body_of(sheet_of(pane)));
    graphene_rect_t summary;
    g_assert_true(gtk_widget_compute_bounds(subject, pane, &summary));
    g_assert_cmpfloat(summary.origin.x + summary.size.width, <=, gtk_widget_get_width(pane));
    g_assert_cmpint(gtk_widget_get_height(thumbnail), ==, 44);
    g_assert_cmpfloat(row.origin.y, >=, panel.origin.y + panel.size.height);
    g_assert_cmpfloat(row.origin.y + row.size.height, <=, gtk_widget_get_height(window));
    luma_action_center_fold(center); settle();
    g_assert_true(gtk_widget_get_mapped(copy));
    g_assert_null(luma_action_center_get_grown(center));
    for (guint n = 0; n < G_N_ELEMENTS(items); n++) g_object_unref(items[n]);
    gtk_window_destroy(GTK_WINDOW(window));
  }
}

int main(int argc, char **argv) {
  gtk_init();
  luma_init();
  g_test_init(&argc, &argv, NULL);
  g_test_add_func("/lumaui/details/embedded", test_embedded);
  g_test_add_func("/lumaui/details-pane/rows", test_rows);
  g_test_add_func("/lumaui/details-pane/photos", test_photos);
  g_test_add_func("/lumaui/details-pane/content", test_content);
  g_test_add_func("/lumaui/details-pane/show", test_show);
  g_test_add_func("/lumaui/details-pane/drawer", test_drawer);
  return g_test_run();
}
