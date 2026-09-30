from thrift_agent.brain.sizes import SEGMENTS, kids_shoe, size_label
from thrift_agent.schema import Ev


def ev(value):
    return Ev(value=value, photos=[3], source="photo", confidence=0.9)


def kid(facts, **kw):
    base = dict(department="Kids", size_printed=ev("7.5"), size_eu=Ev())
    base.update(kw)
    return facts(**base)


def test_kids_shoe_sizes_carry_their_system(facts):
    assert size_label(kid(facts, size_us=ev("7.5"), size_eu=ev("24"))) == "EU 24 / US Toddler 7.5"
    assert size_label(kid(facts, size_us=ev("12C"), size_printed=ev("12C"))) == "US Toddler 12"
    assert size_label(kid(facts, size_us=ev("13C"), size_printed=ev("13C"))) == "US Little Kid 13"
    assert size_label(kid(facts, size_us=ev("4Y"), size_printed=ev("4Y"))) == "US Big Kid 4"
    assert size_label(kid(facts, size_us=ev("4"), size_eu=ev("36"))) == "EU 36 / US Big Kid 4"


def test_other_departments_and_missing_sizes_are_unchanged(facts):
    assert size_label(facts()) == "7.5"                                        # women's
    assert size_label(facts(department="Men", size_us=ev("10"))) == "10"
    assert size_label(facts(size_us=Ev())) is None
    assert size_label(kid(facts, size_us=Ev(), size_eu=ev("24"))) is None


def test_segments_are_poshmarks_groups(facts):
    """Toddler up to 12C (0-7C included: Poshmark's Baby tab, but buyers search "Toddler"), Little Kid 12.5-13.5C and
    1-3Y, Big Kid 3.5-7Y."""
    for size, segment in (("0C", "Toddler"), ("5C", "Toddler"), ("7C", "Toddler"), ("7.5C", "Toddler"),
                          ("10C", "Toddler"), ("10.5C", "Toddler"), ("11C", "Toddler"), ("12C", "Toddler"),
                          ("12.5C", "Little Kid"), ("13.5C", "Little Kid"),
                          ("1Y", "Little Kid"), ("2Y", "Little Kid"), ("3Y", "Little Kid"),
                          ("3.5Y", "Big Kid"), ("4Y", "Big Kid"), ("7Y", "Big Kid")):
        assert size_label(kid(facts, size_us=ev(size))) == f"US {segment} {size[:-1]}", size


def test_the_letter_comes_from_the_size_the_label_the_number_or_the_eu_size(facts):
    assert size_label(kid(facts, size_us=ev("4"), size_printed=ev("4 Y"))) == "US Big Kid 4"    # letter on the label
    assert size_label(kid(facts, size_us=ev("2"), size_printed=ev("US 2Y / EU 33"))) == "EU 33 / US Little Kid 2"
    assert size_label(kid(facts, size_us=ev("11"))) == "US Toddler 11"           # above 7: only a C size can be it
    assert size_label(kid(facts, size_us=ev("13"))) == "US Little Kid 13"
    assert size_label(kid(facts, size_us=ev("12"), size_eu=ev("30"))) == "EU 30 / US Toddler 12"
    assert size_label(kid(facts, size_us=ev("4"), size_eu=ev("20"))) == "EU 20 / US Toddler 4"  # up to 7: the EU size
    assert size_label(kid(facts, size_us=ev("1"), size_eu=ev("34"))) == "EU 34 / US Little Kid 1"
    assert size_label(kid(facts, size_us=ev("4"), size_eu=ev("36"))) == "EU 36 / US Big Kid 4"
    assert size_label(kid(facts, size_us=ev("1"))) == "US Toddler 1"      # a bare 1 (1C or 1Y?) is taken as a C size


def test_eu_size_read_from_the_printed_label(facts):
    printed = kid(facts, size_us=ev("7.5"), size_printed=ev("US 7.5C / EU 24 / 15 cm"))
    assert size_label(printed) == "EU 24 / US Toddler 7.5"                      # 7.5 and 15 are under 16; "cm" is no C
    assert size_label(kid(facts, size_us=ev("7.5"), size_eu=ev("EU 24.0"))) == "EU 24 / US Toddler 7.5"
    assert size_label(kid(facts, size_us=ev("13"), size_printed=ev("13C 31"))) == "EU 31 / US Little Kid 13"


def test_kids_clothing_and_odd_values_stay_as_they_are(facts):
    assert size_label(kid(facts, size_us=ev("5"), category="Tops")) == "5"      # a size-5 tee is not a Toddler 5
    assert size_label(kid(facts, size_us=ev("4T"), category="Dresses")) == "4T"
    assert size_label(kid(facts, size_us=ev("M"))) == "M"                       # not a shoe size
    assert kids_shoe(kid(facts, size_us=ev("5"))) and not kids_shoe(facts())
    assert not kids_shoe(kid(facts, size_us=ev("5"), category="Tops"))
    assert SEGMENTS == ("Toddler", "Little Kid", "Big Kid")


def test_title_size_is_us_only(facts):
    from thrift_agent.brain.sizes import title_size
    kid = facts(department="Kids", category="Shoes",
                size_us=Ev(value="7.5", photos=[1], source="derived", confidence=0.8),
                size_eu=Ev(value="24", photos=[1], source="photo", confidence=0.9))
    assert title_size(kid) == "Toddler size 7.5"
    assert title_size(facts(department="Kids", category="Shoes", size_us=Ev(value="4Y", photos=[1], source="photo",
                                                                            confidence=0.9))) == "Big Kid size 4"
    for size, title in (("11C", "Toddler size 11"), ("5C", "Toddler size 5"), ("13C", "Little Kid size 13"),
                        ("2Y", "Little Kid size 2")):                      # Poshmark's groups (decision, WO10)
        assert title_size(facts(department="Kids", category="Shoes", size_us=ev(size))) == title, size
    adult = facts(size_eu=Ev(value="38", photos=[3], source="photo", confidence=0.9))
    assert title_size(adult) == "size 7.5"
    assert title_size(facts(size_us=Ev(value=None, confidence=0.1))) is None
