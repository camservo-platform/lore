from lore.web.conversation import is_echo, words


def test_words_normalises():
    assert words("The Gate—it's OPEN!") == ["the", "gate", "it's", "open"]


def test_echo_is_the_gm_heard_through_the_mic():
    gm = "The torchlight flickers as you step into the drowned keep. What do you do?"
    assert is_echo("as you step into the drowned keep", gm)
    assert is_echo("", gm)                                   # nothing intelligible
    assert not is_echo("I draw my sword and charge the guard", gm)
    assert not is_echo("stop, I want to go back", gm)
    assert not is_echo("hello", "")                          # GM silent: never echo
