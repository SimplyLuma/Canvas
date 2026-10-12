import os

# Every edit in the tests checks that change tracking saw it: a layer changed
# in place (item.path.append(...)) fails the test that did it.
os.environ.setdefault("CANVAS_VERIFY_CHANGES", "1")
