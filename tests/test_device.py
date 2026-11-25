"""Tests that need no phone: parsing, refs and target resolution."""

from __future__ import annotations

from maple_android_tools.device import Device, Element

XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node class="android.widget.FrameLayout" bounds="[0,0][1080,2400]" clickable="false">
    <node class="android.widget.EditText" text="" resource-id="com.app:id/email"
          bounds="[84,725][766,773]" clickable="true" focused="true" />
    <node class="android.widget.Button" text="Siguiente" resource-id="com.app:id/next"
          bounds="[126,1819][954,1945]" clickable="true" />
    <node class="android.widget.TextView" text="Olvidaste tu contrasena"
          bounds="[63,1999][447,2058]" clickable="false" />
    <node class="android.widget.TextView" text="Siguiente"
          bounds="[63,2100][447,2150]" clickable="false" />
    <node class="android.widget.Switch" text="Wifi" bounds="[900,300][1000,400]"
          clickable="true" checkable="true" checked="true" />
    <node class="android.view.View" bounds="[0,0][0,0]" clickable="true" />
  </node>
</hierarchy>"""


class FakeDevice(Device):
    def __init__(self) -> None:  # no uiautomator2, no adb
        super().__init__(serial="fake")

    def raw_xml(self) -> str:
        return XML

    def screen_size(self):
        return 1080, 2400

    def current_app(self):
        return "com.app", ".Main"

    def keyboard_visible(self):
        return False


def make() -> FakeDevice:
    return FakeDevice()


def test_elements_skip_zero_area_nodes():
    els = make().elements()
    assert all(e.bounds[2] > e.bounds[0] for e in els)


def test_refs_are_sequential_and_centered():
    els = make().elements()
    assert [e.ref for e in els] == list(range(1, len(els) + 1))
    button = next(e for e in els if e.text == "Siguiente")
    assert button.center == ((126 + 954) // 2, (1819 + 1945) // 2)


def test_checked_state_is_reported():
    switch = next(e for e in make().elements() if e.kind == "sw")
    assert switch.checked is True


def test_resolve_by_exact_text_prefers_clickable_node():
    el = make().resolve("text=Siguiente")
    assert el is not None and el.rid == "com.app:id/next"


def test_resolve_by_contains_and_id():
    dev = make()
    assert dev.resolve("~contrasena").text.startswith("Olvidaste")
    assert dev.resolve("id=email").editable is True


def test_resolve_by_coordinates():
    el = make().resolve("540,1200")
    assert el is not None and el.center == (540, 1200)


def test_resolve_missing_returns_none():
    assert make().resolve("text=NoExiste") is None
    assert make().resolve("") is None


def test_ref_survives_a_relayout():
    dev = make()
    els = dev.elements()
    target = next(e for e in els if e.text == "Siguiente")
    # same tree, shifted horizontally: the ref must follow the signature
    global XML
    shifted = XML.replace("[126,1819][954,1945]", "[200,1819][1028,1945]")
    XML = shifted
    try:
        again = dev._resolve_ref(target.ref)
        assert again is not None and again.bounds[0] == 200
    finally:
        XML = XML.replace("[200,1819][1028,1945]", "[126,1819][954,1945]")


def test_snapshot_is_actionable_only_by_default():
    quiet = make().snapshot()
    assert "Olvidaste" not in quiet
    loud = make().snapshot(full=True)
    assert "Olvidaste" in loud


def test_snapshot_does_not_exceed_budget():
    dev = make()
    lines = dev.snapshot().splitlines()
    assert len(lines) <= 62


def test_element_line_marks_capabilities():
    el = Element(1, "Go", "btn", "android.widget.Button", "", (0, 0, 10, 10), True, False)
    assert el.line() == "#1 btn 'Go' tap"
