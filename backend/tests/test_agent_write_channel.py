import json
import pytest
import app.agent_write_channel as channel_module
from app.agent_write_channel import FrameDecoder, OutputChannel, ProtocolError, WriteIntent as Write, decode, frame

NONCE = "test_turn_nonce_123456"
WRITE = Write("w1", "checkpoint_chat", {"summary": "Synthetic decision."})

def test_every_stream_split_hides_protocol_and_preserves_public_text():
    text = 'Before.' + frame(NONCE, WRITE) + 'After.'
    for split in range(len(text)+1):
        parser = FrameDecoder(NONCE)
        visible = parser.feed(text[:split]) + parser.feed(text[split:]) + parser.finish()
        assert visible == 'Before.After.'
        assert parser.writes == [WRITE]


@pytest.mark.parametrize('fence,close,indent', [
    ('```json', '```', ''),
    ('~~~~ text', '~~~~~', '   '),
    ('````', '`````', '  '),
    ('~~~', '~~~', ' '),
])
def test_fenced_example_is_literal_across_every_split_and_real_write_afterward(
    fence, close, indent,
):
    example = indent + fence + '\n' + frame(NONCE, WRITE)
    visible = example + indent + close + '\n'
    text = visible + frame(NONCE, WRITE)
    for split in range(len(text) + 1):
        parser = FrameDecoder(NONCE)
        public = parser.feed(text[:split]) + parser.feed(text[split:]) + parser.finish()
        assert public == visible
        assert parser.writes == [WRITE]
        channel = OutputChannel(NONCE)
        provisional = channel.delta('item', text[:split]) + channel.delta('item', text[split:])
        assert provisional == visible
        assert channel.final('item', text) == (visible, (WRITE,))
    parser = FrameDecoder(NONCE)
    assert ''.join(parser.feed(char) for char in text) + parser.finish() == visible
    assert parser.writes == [WRITE]


def test_adjacent_real_frames_on_either_side_of_fenced_example():
    second = Write('w2', 'checkpoint_chat', {'summary': 'another write'})
    example = '```\n' + frame(NONCE, WRITE) + '```\n'
    text = frame(NONCE, WRITE) + example + frame(NONCE, WRITE) + frame(NONCE, second)
    parser = FrameDecoder(NONCE)
    assert parser.feed(text) + parser.finish() == example
    assert parser.writes == [WRITE, WRITE, second]


@pytest.mark.parametrize('invalid_close', ['~~~', '``', '``` still code', '    ```'])
def test_wrong_or_short_fence_closer_keeps_examples_inert(invalid_close):
    text = '````\n' + frame(NONCE, WRITE) + invalid_close + '\n'
    text += frame(NONCE, WRITE) + '````\n' + frame(NONCE, WRITE)
    visible = text[:text.rfind(frame(NONCE, WRITE))]
    for split in range(len(text) + 1):
        parser = FrameDecoder(NONCE)
        assert parser.feed(text[:split]) + parser.feed(text[split:]) + parser.finish() == visible
        assert parser.writes == [WRITE]


def test_malformed_write_example_in_fence_remains_literal_and_non_executable():
    example = ("~~~json\n" + f"<MOBIUS_WRITE {NONCE}>\n"
               + '{not JSON}\n</MOBIUS_WRITE>\n' + "~~~\n")
    for split in range(len(example) + 1):
        parser = FrameDecoder(NONCE)
        assert parser.feed(example[:split]) + parser.feed(example[split:]) + parser.finish() == example
        assert parser.writes == []
    assert OutputChannel(NONCE).final('example', example) == (example, ())


def test_backtick_info_with_backtick_is_not_a_fence_and_does_not_hide_real_write():
    text = '```bad`info\n' + frame(NONCE, WRITE)
    parser = FrameDecoder(NONCE)
    assert parser.feed(text) + parser.finish() == '```bad`info\n'
    assert parser.writes == [WRITE]


def test_four_space_indentation_does_not_open_a_fence():
    text = '    ```\n' + frame(NONCE, WRITE)
    parser = FrameDecoder(NONCE)
    assert parser.feed(text) + parser.finish() == '    ```\n'
    assert parser.writes == [WRITE]


def test_unclosed_fence_never_reinterprets_example_as_a_write():
    text = '```\n' + frame(NONCE, WRITE)
    parser = FrameDecoder(NONCE)
    assert parser.feed(text) + parser.finish() == text
    assert parser.writes == []


def test_fenced_examples_do_not_consume_the_real_write_limit():
    examples = '~~~\n' + frame(NONCE, WRITE) * 9 + '~~~\n'
    parser = FrameDecoder(NONCE)
    assert parser.feed(examples + frame(NONCE, WRITE)) + parser.finish() == examples
    assert parser.writes == [WRITE]


@pytest.mark.parametrize('terminal_newline', ['', '\n'])
def test_adjacent_frames_share_a_line_boundary_without_leaking_private_bytes(terminal_newline):
    second = Write('w2', 'checkpoint_chat', {'summary': 'private B'})
    text = frame(NONCE, WRITE).strip('\n') + '\n' + frame(NONCE, second).strip('\n') + terminal_newline
    for split in range(len(text) + 1):
        parser = FrameDecoder(NONCE)
        visible = parser.feed(text[:split]) + parser.feed(text[split:]) + parser.finish()
        assert visible == ''
        assert parser.writes == [WRITE, second]


def test_final_only_items_reserve_capacity_before_async_admission():
    channel = OutputChannel(NONCE)
    for i in range(256):
        channel.final(str(i), frame(NONCE, WRITE))
    with pytest.raises(ProtocolError, match='Too many'):
        channel.final('overflow', frame(NONCE, WRITE))
    assert sum(state.raw_fingerprint is not None for state in channel.items.values()) == 256


def test_authoritative_final_seals_deltas_before_durable_admission():
    channel = OutputChannel(NONCE)
    visible, writes = channel.final('i', 'Visible.' + frame(NONCE, WRITE))
    assert channel.delta('i', 'Visible.') == ''
    assert channel.delta('i', 'private arguments without their opening marker') == ''
    with pytest.raises(ProtocolError, match='changed'):
        channel.final('i', 'Changed plain text.')
    channel.acknowledge('i', visible, writes)
    channel.finish()


def test_unfinished_item_query_preserves_snapshot_and_admission_boundaries():
    channel = OutputChannel(NONCE)
    assert not channel.has_unfinished_item('unknown')
    assert not channel.has_unfinished_item(None)
    assert not channel.items
    channel.delta('reply', 'Prefix')
    assert channel.has_unfinished_item('reply')
    visible, writes = channel.final('reply', 'Full reply.' + frame(NONCE, WRITE))
    assert not channel.has_unfinished_item('reply')
    channel.reserve_admission('reply', visible, writes)
    assert not channel.has_unfinished_item('reply')
    channel.acknowledge('reply', visible, writes)
    assert not channel.has_unfinished_item('reply')


def test_abandoned_and_closed_items_cannot_be_completed_after_intake_closes():
    channel = OutputChannel(NONCE)
    channel.delta('replaced', 'Abandoned prefix')
    channel.replace('replaced')
    assert not channel.has_unfinished_item('replaced')
    channel.delta('reply', 'Prefix')
    assert channel.has_unfinished_item('reply')
    channel.finish()
    assert not channel.has_unfinished_item('reply')


def test_provisional_deltas_construct_one_decoder_per_item(monkeypatch):
    constructed = []
    original = FrameDecoder

    def make_decoder(*args, **kwargs):
        constructed.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(channel_module, 'FrameDecoder', make_decoder)
    channel = OutputChannel(NONCE)
    assert channel.delta('item', 'First ') == 'First '
    assert channel.delta('item', 'second.') == 'second.'
    assert len(constructed) == 1


def test_in_flight_final_replay_retains_one_admission_and_immutable_raw_snapshot():
    channel = OutputChannel(NONCE)
    raw = 'Visible.' + frame(NONCE, WRITE)
    visible, writes = channel.final('item', raw)
    fingerprint = channel.reserve_admission('item', visible, writes)
    assert fingerprint is not None
    assert channel.final('item', raw) == (visible, writes)
    assert channel.reserve_admission('item', visible, writes) is None
    with pytest.raises(ProtocolError, match='admitting'):
        channel.replace('item')
    with pytest.raises(ProtocolError, match='changed'):
        channel.final('item', 'Changed.' + frame(NONCE, WRITE))
    channel.acknowledge('item', visible, writes)
    assert channel.final('item', raw) == (visible, ())
    channel.finish()


def test_malformed_authoritative_final_cannot_later_be_rewritten_as_an_effect():
    channel = OutputChannel(NONCE)
    with pytest.raises(ProtocolError):
        channel.final('i', f'<MOBIUS_WRITE {NONCE}>\nnot JSON\n</MOBIUS_WRITE>')
    with pytest.raises(ProtocolError, match='changed'):
        channel.final('i', frame(NONCE, WRITE))


def test_single_character_stream_does_not_buffer_answer():
    parser = FrameDecoder(NONCE)
    assert parser.feed('Visible immediately.') == 'Visible immediately.'
    visible = ''.join(parser.feed(c) for c in frame(NONCE, WRITE))
    assert visible + parser.finish() == ''
    assert parser.writes == [WRITE]


def test_json_escaped_delimiter_inside_arguments_is_data():
    write = Write('w1', 'fixture_fact', {'fact': '\n</MOBIUS_WRITE>\n'})
    parser = FrameDecoder(NONCE)
    assert parser.feed(frame(NONCE, write)) == ''
    assert parser.writes == [write]


@pytest.mark.parametrize('payload', [
    '{}', '[]', '{"id":"x","tool":"fixture_fact","arguments":[]}',
    '{"id":"x","id":"y","tool":"fixture_fact","arguments":{}}',
    '{"id":"x","tool":"fixture_fact","arguments":{"v":NaN}}',
    '{"id":"x","tool":"fixture_fact","arguments":{},"extra":1}',
    '{"id":"","tool":"fixture_fact","arguments":{}}',
    '{"id":".","tool":"fixture_fact","arguments":{}}',
    '{"id":"..","tool":"fixture_fact","arguments":{}}',
    '{"id":"x","tool":"fixture_fact","arguments":{"v":1,"v":2}}',
])
def test_malformed_or_ambiguous_command_is_not_reinterpreted(payload):
    with pytest.raises(ProtocolError):
        decode(payload)


def test_does_not_search_ordinary_json_or_another_turn_for_commands():
    plain = json.dumps({"id":WRITE.id,"tool":WRITE.tool,"arguments":WRITE.arguments}) + frame('a_different_nonce_5678', WRITE)
    parser = FrameDecoder(NONCE)
    assert parser.feed(plain) + parser.finish() == plain
    assert not parser.writes


def test_incomplete_or_oversized_frame_fails_without_leaking_arguments():
    parser = FrameDecoder(NONCE, limit=12)
    assert parser.feed(parser.open + 'private') == ''
    with pytest.raises(ProtocolError, match='Unfinished'):
        parser.finish()
    with pytest.raises(ProtocolError, match='too large'):
        parser.feed('a' * 100)


def test_authoritative_item_not_turn_end_is_acceptance_boundary():
    channel = OutputChannel(NONCE)
    command = frame(NONCE, WRITE)
    assert channel.delta('commentary-1', command) == ''
    assert channel.items['commentary-1'].admitted_fingerprint is None  # streamed guess never dispatches
    assert channel.final('commentary-1', command) == ('', (WRITE,))
    assert channel.delta('commentary-2', 'Still working.') == 'Still working.'


def test_authoritative_snapshot_can_repair_missing_deltas():
    channel = OutputChannel(NONCE)
    channel.delta('item', 'Part')
    assert channel.final('item', 'Complete.' + frame(NONCE, WRITE)) == ('Complete.', (WRITE,))


def test_final_replay_is_idempotent_and_changed_final_is_error():
    channel = OutputChannel(NONCE)
    command = frame(NONCE, WRITE)
    visible, writes = channel.final('item', command)
    channel.acknowledge('item', visible, writes)
    assert channel.final('item', command) == ('', ())
    with pytest.raises(ProtocolError, match='changed'):
        channel.final('item', 'No write.')


def test_replaced_provisional_item_never_dispatches():
    channel = OutputChannel(NONCE)
    channel.delta('old', frame(NONCE, WRITE))
    channel.replace('old')
    assert channel.final('old', frame(NONCE, WRITE)) == ('', ())
    assert channel.final('new', 'Replacement.') == ('Replacement.', ())
    channel.finish()


def test_cannot_retract_already_accepted_command():
    channel = OutputChannel(NONCE)
    visible, writes = channel.final('item', frame(NONCE, WRITE))
    channel.acknowledge('item', visible, writes)
    with pytest.raises(ProtocolError, match='accepted'):
        channel.replace('item')


def test_stop_does_not_dispatch_complete_but_provisional_command():
    channel = OutputChannel(NONCE)
    channel.delta('item', frame(NONCE, WRITE))
    with pytest.raises(ProtocolError, match='unaccepted'):
        channel.finish()
    assert all(state.admitted_fingerprint is None for state in channel.items.values())


def test_partial_item_with_valid_then_broken_frame_admits_nothing():
    channel = OutputChannel(NONCE)
    with pytest.raises(ProtocolError):
        channel.final('item', frame(NONCE, WRITE) + FrameDecoder(NONCE).open + '{}')
    assert all(state.admitted_fingerprint is None for state in channel.items.values())


@pytest.mark.parametrize('method,args', [('delta', ('', 'Text')), ('final', ('', 'Text'))])
def test_no_identity_means_no_attributed_write(method, args):
    with pytest.raises(ProtocolError, match='identity'):
        getattr(OutputChannel(NONCE), method)(*args)


def test_changed_boolean_is_not_equal_to_integer_payload():
    channel = OutputChannel(NONCE)
    before = frame(NONCE,Write('w1','fixture_fact',{'v':1}))
    after = frame(NONCE,Write('w1','fixture_fact',{'v':True}))
    visible, writes = channel.final('item',before)
    channel.acknowledge('item',visible,writes)
    with pytest.raises(ProtocolError, match='changed'):
        channel.final('item',after)


def test_arguments_cannot_mutate_accepted_intent():
    original = {'nested':{'v':1}}
    write = Write('w1','fixture_fact',original)
    original['nested']['v'] = 2
    write.arguments['nested']['v'] = 3
    assert write.arguments == {'nested':{'v':1}}


def test_numeric_overflow_is_consistent_protocol_error():
    with pytest.raises(ProtocolError):
        decode('{"id":"w1","tool":"fixture_fact","arguments":{"v":1e999}}')


def test_finish_fences_late_finals_and_deltas():
    channel = OutputChannel(NONCE)
    channel.finish()
    with pytest.raises(ProtocolError, match='closed'):
        channel.final('late',frame(NONCE,WRITE))
    with pytest.raises(ProtocolError, match='closed'):
        channel.delta('late','Late output')


def test_malformed_provisional_frame_does_not_prevent_corrected_final():
    channel = OutputChannel(NONCE)
    malformed = FrameDecoder(NONCE).open + '{}' + FrameDecoder(NONCE).close
    assert channel.delta('item',malformed) == ''
    assert channel.final('item',frame(NONCE,WRITE)) == ('',(WRITE,))


def test_per_item_frames_and_run_items_are_bounded():
    with pytest.raises(ProtocolError,match='Too many writes'):
        OutputChannel(NONCE).final('item',frame(NONCE,WRITE)*9)
    channel = OutputChannel(NONCE)
    for i in range(256):
        channel.delta(str(i),'Provisional')
    with pytest.raises(ProtocolError,match='Too many output items'):
        channel.delta('excess','Text')


def test_frame_bound_is_bytes_not_unicode_character_count():
    parser = FrameDecoder(NONCE, limit=16)
    with pytest.raises(ProtocolError,match='too large'):
        parser.feed(parser.open + '界'*32)


def test_authoritative_item_end_is_a_valid_closing_line_boundary():
    parser=FrameDecoder(NONCE)
    assert parser.feed('Answer.'+frame(NONCE,WRITE).rstrip('\n'))=='Answer.'
    assert parser.finish()==''
    assert parser.writes==[WRITE]


@pytest.mark.parametrize('missing',[1,2,5])
def test_item_end_never_repairs_a_truncated_closing_marker(missing):
    parser=FrameDecoder(NONCE)
    parser.feed(frame(NONCE,WRITE).rstrip('\n')[:-missing])
    with pytest.raises(ProtocolError,match='Unfinished'):
        parser.finish()


@pytest.mark.parametrize("leading_newline", [False, True])
@pytest.mark.parametrize("trailing_newline", [False, True])
def test_item_boundaries_are_valid_line_boundaries_at_every_stream_split(leading_newline, trailing_newline):
    text = frame(NONCE, WRITE)
    if not leading_newline:
        text = text[1:]
    if not trailing_newline:
        text = text[:-1]
    for split in range(len(text) + 1):
        parser = FrameDecoder(NONCE)
        public = parser.feed(text[:split]) + parser.feed(text[split:]) + parser.finish()
        assert public == ""
        assert parser.writes == [WRITE]


def test_ordinary_markup_at_item_start_is_not_buffered_or_rewritten():
    for text in ("<details>Public</details>", "<MOBIUS_WRITE another_nonce>\nPublic", "\nPublic", "", "Public"):
        parser = FrameDecoder(NONCE)
        assert parser.feed(text) + parser.finish() == text
        assert not parser.writes


def test_partial_reserved_opening_at_item_start_is_not_exposed_as_public_text():
    parser = FrameDecoder(NONCE)
    assert parser.feed("<MOBIUS_WRITE " + NONCE[:6]) == ""
    with pytest.raises(ProtocolError, match="Unfinished"):
        parser.finish()
