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
    assert size_label(kid(facts, size_us=ev("12C"), size_printed=ev("12C"))) == "US Little Kid 12"
    assert size_label(kid(facts, size_us=ev("4Y"), size_printed=ev("4Y"))) == "US Big Kid 4"
    assert size_label(kid(facts, size_us=ev("4"), size_eu=ev("36"))) == "EU 36 / US Big Kid 4"


def test_other_departments_and_missing_sizes_are_unchanged(facts):
    assert size_label(facts()) == "7.5"                                        # women's
    assert size_label(facts(department="Men", size_us=ev("10"))) == "10"
    assert size_label(facts(size_us=Ev())) is None
    assert size_label(kid(facts, size_us=Ev(), size_eu=ev("24"))) is None


def test_segment_from_the_letter_then_the_eu_size_then_the_number(facts):
    assert size_label(kid(facts, size_us=ev("10C"))) == "US Toddler 10"           # C up to 10 is Toddler
    assert size_label(kid(facts, size_us=ev("10.5C"))) == "US Little Kid 10.5"
    assert size_label(kid(facts, size_us=ev("1Y"))) == "US Big Kid 1"
    assert size_label(kid(facts, size_us=ev("4"), size_printed=ev("4 Y"))) == "US Big Kid 4"    # letter on the label
    assert size_label(kid(facts, size_us=ev("4"), size_eu=ev("20"))) == "EU 20 / US Toddler 4"  # the EU size decides
    assert size_label(kid(facts, size_us=ev("12"), size_eu=ev("30"))) == "EU 30 / US Little Kid 12"
    assert size_label(kid(facts, size_us=ev("1"), size_eu=ev("34"))) == "EU 34 / US Big Kid 1"
    assert size_label(kid(facts, size_us=ev("10"))) == "US Toddler 10"           # no letter, no EU: the number
    assert size_label(kid(facts, size_us=ev("10.5"))) == "US Little Kid 10.5"
    assert size_label(kid(facts, size_us=ev("13.5"))) == "US Little Kid 13.5"
    assert size_label(kid(facts, size_us=ev("1"))) == "US Toddler 1"      # a bare 1 (1C or 1Y?) falls to the number rule
    assert size_label(kid(facts, size_us=ev("14"))) == "US Big Kid 14"


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
