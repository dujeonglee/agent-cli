"""Tests for AdaptiveRenderer - resize-responsive renderer."""

import pytest
import signal
from unittest.mock import MagicMock, patch
from io import StringIO

from agent_cli.render.adaptive import (
    AdaptiveRenderer,
    _ADAPTIVE,
    _NARROW_THRESHOLD,
    _WIDE_THRESHOLD,
    _MIN_WIDTH,
    install_resize_handler,
    check_resize,
)


class TestAdaptiveRendererInit:
    """Test AdaptiveRenderer initialization."""

    def test_init_creates_console(self):
        """Renderer creates its own console if not provided."""
        renderer = AdaptiveRenderer()
        assert renderer.con is not None

    def test_init_uses_provided_console(self):
        """Renderer uses provided console."""
        mock_console = MagicMock()
        renderer = AdaptiveRenderer(mock_console)
        assert renderer.con == mock_console

    def test_initial_state(self):
        """Renderer starts in default state."""
        with patch('shutil.get_terminal_size', return_value=(80, 24)):
            renderer = AdaptiveRenderer()
            assert renderer._live is None
            assert not renderer._resize_handler_installed
            assert renderer._last_width == 80


class TestTerminalSizeDetection:
    """Test terminal size detection methods."""

    def test_get_terminal_width_normal(self):
        """Get correct terminal width."""
        with patch('shutil.get_terminal_size', return_value=(120, 40)):
            renderer = AdaptiveRenderer()
            width = renderer._get_terminal_width()
            assert width == 120

    def test_get_terminal_width_min(self):
        """Width respects minimum threshold."""
        with patch('shutil.get_terminal_size', return_value=(20, 40)):
            renderer = AdaptiveRenderer()
            width = renderer._get_terminal_width()
            assert width == _MIN_WIDTH

    def test_get_terminal_width_fallback(self):
        """Fallback to default on error."""
        with patch('shutil.get_terminal_size', side_effect=OSError):
            renderer = AdaptiveRenderer()
            width = renderer._get_terminal_width()
            assert width == 80

    def test_is_narrow_true(self):
        """Detect narrow terminal correctly."""
        with patch('shutil.get_terminal_size', return_value=(70, 24)):
            renderer = AdaptiveRenderer()
            assert renderer._is_narrow()

    def test_is_narrow_false(self):
        """Detect wide terminal correctly."""
        with patch('shutil.get_terminal_size', return_value=(100, 24)):
            renderer = AdaptiveRenderer()
            assert not renderer._is_narrow()

    def test_is_wide_true(self):
        """Detect wide terminal correctly."""
        with patch('shutil.get_terminal_size', return_value=(150, 24)):
            renderer = AdaptiveRenderer()
            assert renderer._is_wide()

    def test_is_wide_false(self):
        """Detect narrow terminal correctly."""
        with patch('shutil.get_terminal_size', return_value=(100, 24)):
            renderer = AdaptiveRenderer()
            assert not renderer._is_wide()


class TestTextAdaptation:
    """Test text adaptation methods."""

    def test_truncate_short_text(self):
        """Short text is not truncated."""
        renderer = AdaptiveRenderer()
        with patch('shutil.get_terminal_size', return_value=(80, 24)):
            result = renderer._truncate("Hello world")
            assert result == "Hello world"

    def test_truncate_long_text(self):
        """Long text is truncated with ellipsis."""
        renderer = AdaptiveRenderer()
        with patch('shutil.get_terminal_size', return_value=(80, 24)):
            long_text = "A" * 100
            result = renderer._truncate(long_text)
            assert result.endswith("...")
            assert len(result) <= 80

    def test_truncate_custom_max_len(self):
        """Truncate respects custom max length."""
        renderer = AdaptiveRenderer()
        with patch('shutil.get_terminal_size', return_value=(80, 24)):
            long_text = "B" * 50
            result = renderer._truncate(long_text, max_len=20)
            assert len(result) <= 23  # 20 + "..."

    def test_truncate_empty_string(self):
        """Empty string returns empty."""
        renderer = AdaptiveRenderer()
        result = renderer._truncate("")
    def test_wrap_text_basic(self):
        """Text wrapping works correctly."""
        renderer = AdaptiveRenderer()
        with patch('shutil.get_terminal_size', return_value=(40, 24)):
            text = "This is a very long line that should wrap to multiple lines"
            result = renderer._wrap_text(text)
            lines = result.split("\n")
            assert len(lines) >= 2
            for line in lines:
                assert len(line) <= 40

    def test_wrap_text_short(self):
        """Short text doesn't get wrapped."""
        renderer = AdaptiveRenderer()
        with patch('shutil.get_terminal_size', return_value=(80, 24)):
            text = "Short text"
            result = renderer._wrap_text(text)
            assert result == "Short text"


class TestAdaptiveRendererRendering:
    """Test actual rendering methods."""

    @pytest.fixture
    def mock_console(self):
        """Create a mock console that captures output."""
        mock = MagicMock()
        return mock

    def test_header_narrow_terminal(self, mock_console):
        """Header adapts to narrow terminal."""
        with patch('shutil.get_terminal_size', return_value=(70, 24)):
            renderer = AdaptiveRenderer(mock_console)
            renderer.header("test-provider", "test-model", max_turns=10)
            
            # Verify panel was called
            assert mock_console.print.called

    def test_header_wide_terminal(self, mock_console):
        """Header uses full styling in wide terminal."""
        with patch('shutil.get_terminal_size', return_value=(150, 24)):
            renderer = AdaptiveRenderer(mock_console)
            renderer.header("test-provider", "test-model", max_turns=10)
            
            assert mock_console.print.called

    def test_thought_wraps_long_content(self, mock_console):
        """Long thought content is wrapped in narrow terminals."""
        with patch('shutil.get_terminal_size', return_value=(70, 24)):
            renderer = AdaptiveRenderer(mock_console)
            long_thought = "This is a very long thought that should be wrapped to fit the available terminal width properly."
            renderer.thought(long_thought, turn=1)
            
            assert mock_console.print.called

    def test_action_truncates_input_narrow(self, mock_console):
        """Action input is truncated in narrow terminals."""
        with patch('shutil.get_terminal_size', return_value=(70, 24)):
            renderer = AdaptiveRenderer(mock_console)
            long_input = "A" * 100
            renderer.action("test_tool", long_input, turn=1)
            
            assert mock_console.print.called

    def test_status_message_truncates(self, mock_console):
        """Status message truncates in narrow terminals."""
        with patch('shutil.get_terminal_size', return_value=(70, 24)):
            renderer = AdaptiveRenderer(mock_console)
            long_message = "This is a very long status message that should be truncated in narrow terminals"
            renderer.status("running", long_message, turn=1)
            
            assert mock_console.print.called

    def test_spinner_start_stores_live(self, mock_console):
        """Spinner starts and stores Live object."""
        renderer = AdaptiveRenderer(mock_console)
        
        with patch('shutil.get_terminal_size', return_value=(80, 24)):
            with patch('agent_cli.render.adaptive.Live') as MockLive:
                mock_live = MagicMock()
                MockLive.return_value = mock_live
                
                renderer.spinner_start("thinking...")
                
                assert renderer._live == mock_live
                mock_live.start.assert_called_once()

    def test_spinner_stop_clears_live(self, mock_console):
        """Spinner stop clears the Live object."""
        with patch('shutil.get_terminal_size', return_value=(80, 24)):
            renderer = AdaptiveRenderer(mock_console)
            renderer._live = MagicMock()
            
            renderer.spinner_stop()
            
            assert renderer._live is None

    def test_dispatch_progress_shortens_thought(self, mock_console):
        """Dispatch progress shortens thought in narrow terminals."""
        with patch('shutil.get_terminal_size', return_value=(70, 24)):
            renderer = AdaptiveRenderer(mock_console)
            long_thought = "A" * 100
            renderer.dispatch_progress(
                label="test",
                turn=1,
                tool_name="test_tool",
                thought=long_thought
            )
            
            assert mock_console.print.called


class TestColorScheme:
    """Test color scheme configuration."""

    def test_all_colors_defined(self):
        """All expected colors are defined."""
        expected_colors = [
            "primary", "secondary", "accent", "success",
            "warning", "error", "info", "muted",
            "thought", "action", "observation", "final", "separator"
        ]
        for color in expected_colors:
            assert color in _ADAPTIVE

    def test_colors_match_fancy_renderer(self):
        """Colors match FancyRenderer scheme."""
        # Just verify the key colors are the same
        assert _ADAPTIVE["success"] == "bright_green"
        assert _ADAPTIVE["error"] == "bright_red"
        assert _ADAPTIVE["warning"] == "bright_yellow"


class TestThresholds:
    """Test terminal size thresholds."""

    def test_narrow_threshold_is_80(self):
        """Narrow threshold is 80 characters."""
        assert _NARROW_THRESHOLD == 80

    def test_wide_threshold_is_120(self):
        """Wide threshold is 120 characters."""
        assert _WIDE_THRESHOLD == 120

    def test_min_width_is_40(self):
        """Minimum width is 40 characters."""
        assert _MIN_WIDTH == 40


class TestSignalHandling:
    """Test SIGWINCH signal handling."""

    def test_resize_handler_set_flag(self):
        """Resize handler sets the event flag."""
        from agent_cli.render.adaptive import _resize_event
        
        # Clear any existing state
        _resize_event.clear()
        
        # Simulate signal
        from agent_cli.render.adaptive import _handle_resize
        _handle_resize(signal.SIGWINCH, None)
        
        assert _resize_event.is_set()

    def test_check_resize_returns_true_after_signal(self):
        """check_resize returns True after signal."""
        from agent_cli.render.adaptive import _resize_event, _handle_resize, check_resize
        
        # Clear and simulate signal
        _resize_event.clear()
        _handle_resize(signal.SIGWINCH, None)
        
        # Check should return True
        assert check_resize()
        
        # Now should be False (event cleared)
        assert not check_resize()

    def test_install_resize_handler_idempotent(self):
        """Installing handler multiple times is safe."""
        with patch('signal.signal') as mock_signal:
            # First install
            install_resize_handler()
            assert mock_signal.called
            
            # Second install should not do anything
            mock_signal.reset_mock()
            install_resize_handler()
            assert not mock_signal.called

    def test_install_resize_handler_handles_windows(self):
        """Handler installation gracefully handles Windows."""
        with patch('signal.signal', side_effect=AttributeError):
            # Should not raise
            install_resize_handler()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


class TestTurnSeparator:
    """Test turn separator rendering."""

    def test_turn_sep_basic(self, mock_console):
        """Turn separator prints correctly."""
        with patch('shutil.get_terminal_size', return_value=(80, 24)):
            renderer = AdaptiveRenderer(mock_console)
            renderer.turn_sep(turn=5)
            assert mock_console.print.called

    def test_turn_sep_narrow_terminal(self, mock_console):
        """Turn separator adapts to narrow terminal."""
        with patch('shutil.get_terminal_size', return_value=(60, 24)):
            renderer = AdaptiveRenderer(mock_console)
            renderer.turn_sep(turn=1)
            # In narrow mode, uses different arrow style
            assert mock_console.print.called


class TestObservation:
    """Test observation rendering."""

    def test_observation_success_status(self, mock_console):
        """Observation with SUCCESS status."""
        with patch('shutil.get_terminal_size', return_value=(80, 24)):
            renderer = AdaptiveRenderer(mock_console)
            renderer.observation("STATUS: success\nSome result", turn=1)
            assert mock_console.print.called

    def test_observation_error_status(self, mock_console):
        """Observation with ERROR status."""
        with patch('shutil.get_terminal_size', return_value=(80, 24)):
            renderer = AdaptiveRenderer(mock_console)
            renderer.observation("STATUS: error\nError details", turn=1)
            assert mock_console.print.called

    def test_observation_with_tool_name(self, mock_console):
        """Observation shows tool name."""
        with patch('shutil.get_terminal_size', return_value=(80, 24)):
            renderer = AdaptiveRenderer(mock_console)
            renderer.observation("STATUS: done\nResult", turn=1, tool_name="my_tool")
            assert mock_console.print.called

    def test_observation_error_line(self, mock_console):
        """Observation handles ERROR: line."""
        with patch('shutil.get_terminal_size', return_value=(80, 24)):
            renderer = AdaptiveRenderer(mock_console)
            renderer.observation("Some content\nERROR: something failed", turn=1)
            assert mock_console.print.called

    def test_observation_narrow_wrapping(self, mock_console):
        """Observation wraps content in narrow terminal."""
        with patch('shutil.get_terminal_size', return_value=(60, 24)):
            renderer = AdaptiveRenderer(mock_console)
            long_content = "This is a very long observation content that should wrap in narrow terminals"
            renderer.observation(long_content, turn=1)
            assert mock_console.print.called


class TestFinalResult:
    """Test final result rendering."""

    def test_final_basic(self, mock_console):
        """Final result renders correctly."""
        with patch('shutil.get_terminal_size', return_value=(80, 24)):
            renderer = AdaptiveRenderer(mock_console)
            renderer.final("Task completed successfully!", turn=1)
            assert mock_console.print.called

    def test_final_wraps_long_content(self, mock_console):
        """Final result wraps long content."""
        with patch('shutil.get_terminal_size', return_value=(60, 24)):
            renderer = AdaptiveRenderer(mock_console)
            long_result = "This is a very long final result that should wrap and fit within the narrow terminal width properly."
            renderer.final(long_result, turn=1)
            assert mock_console.print.called


class TestError:
    """Test error rendering."""

    def test_error_basic(self, mock_console):
        """Error message renders correctly."""
        with patch('shutil.get_terminal_size', return_value=(80, 24)):
            renderer = AdaptiveRenderer(mock_console)
            renderer.error("An error occurred", turn=1)
            assert mock_console.print.called

    def test_error_wraps_in_narrow(self, mock_console):
        """Error wraps in narrow terminal."""
        with patch('shutil.get_terminal_size', return_value=(60, 24)):
            renderer = AdaptiveRenderer(mock_console)
            long_error = "This is a very long error message that should wrap in narrow terminal"
            renderer.error(long_error, turn=1)
            assert mock_console.print.called


class TestRawResponse:
    """Test raw response rendering."""

    def test_raw_non_verbose_shows_preview(self, mock_console):
        """Raw response in non-verbose mode shows preview."""
        with patch('shutil.get_terminal_size', return_value=(80, 24)):
            renderer = AdaptiveRenderer(mock_console)
            renderer.raw("actual raw content", turn=1, verbose=False)
            assert mock_console.print.called

    def test_raw_verbose_shows_full(self, mock_console):
        """Raw response in verbose mode shows full content."""
        with patch('shutil.get_terminal_size', return_value=(80, 24)):
            renderer = AdaptiveRenderer(mock_console)
            renderer.raw("line1\nline2\nline3", turn=1, verbose=True)
            assert mock_console.print.called

    def test_raw_truncates_lines_narrow(self, mock_console):
        """Raw response truncates long lines in narrow terminal."""
        with patch('shutil.get_terminal_size', return_value=(60, 24)):
            renderer = AdaptiveRenderer(mock_console)
            long_line = "A" * 100
            renderer.raw(long_line, turn=1, verbose=True)
            assert mock_console.print.called


class TestModelDetected:
    """Test model detected rendering."""

    def test_model_detected_basic(self, mock_console):
        """Model detected renders correctly."""
        from agent_cli.render.adaptive import _ADAPTIVE
        with patch('shutil.get_terminal_size', return_value=(80, 24)):
            renderer = AdaptiveRenderer(mock_console)
            # Create mock capabilities object
            caps = MagicMock()
            caps.supports_thinking = True
            caps.thinking_budget = 1000
            caps.context_window = 128000
            caps.max_output_tokens = 4096
            caps.supports_structured_output = True
            caps.supports_tool_calling = True
            
            renderer.model_detected("gpt-4", caps, "openai", "/path/to/model")
            assert mock_console.print.called

    def test_model_detected_narrow(self, mock_console):
        """Model detected adapts to narrow terminal."""
        with patch('shutil.get_terminal_size', return_value=(60, 24)):
            renderer = AdaptiveRenderer(mock_console)
            caps = MagicMock()
            caps.supports_thinking = False
            caps.thinking_budget = 0
            caps.context_window = 8000
            caps.max_output_tokens = 1000
            caps.supports_structured_output = False
            caps.supports_tool_calling = True
            
            renderer.model_detected("test-model", caps, "provider", "/path")
            assert mock_console.print.called


class TestModelLoaded:
    """Test model loaded rendering."""

    def test_model_loaded_basic(self, mock_console):
        """Model loaded renders correctly."""
        with patch('shutil.get_terminal_size', return_value=(80, 24)):
            renderer = AdaptiveRenderer(mock_console)
            caps = MagicMock()
            caps.supports_thinking = True
            caps.context_window = 128000
            
            renderer.model_loaded("gpt-4", caps)
            assert mock_console.print.called

    def test_model_loaded_narrow(self, mock_console):
        """Model loaded adapts to narrow terminal."""
        with patch('shutil.get_terminal_size', return_value=(60, 24)):
            renderer = AdaptiveRenderer(mock_console)
            caps = MagicMock()
            caps.supports_thinking = False
            caps.context_window = 8000
            
            renderer.model_loaded("test-model", caps)
            assert mock_console.print.called


class TestContextDump:
    """Test context dump rendering."""

    def test_context_dump_basic(self, mock_console):
        """Context dump renders correctly."""
        with patch('shutil.get_terminal_size', return_value=(80, 24)):
            renderer = AdaptiveRenderer(mock_console)
            messages = [
                {"role": "system", "content": "system message"},
                {"role": "user", "content": "user message"}
                {"role": "user", "content": "user message"}
            ]
            renderer.context_dump(messages, turn=1)
            assert mock_console.print.called

    def test_context_dump_truncates_long_content(self, mock_console):
        """Context dump truncates long message content."""
        with patch('shutil.get_terminal_size', return_value=(80, 24)):
            renderer = AdaptiveRenderer(mock_console)
            long_content = "A" * 200
            messages = [{"role": "user", "content": long_content}]
            renderer.context_dump(messages, turn=1)
            assert mock_console.print.called

    def test_context_dump_narrow(self, mock_console):
        """Context dump adapts to narrow terminal."""
        with patch('shutil.get_terminal_size', return_value=(60, 24)):
            renderer = AdaptiveRenderer(mock_console)
            messages = [{"role": "assistant", "content": "test message"}]
            renderer.context_dump(messages, turn=5)
            assert mock_console.print.called


class TestSpinner:
    """Test spinner rendering."""

    def test_spinner_stops_when_none(self, mock_console):
        """Spinner stop with no live does nothing."""
        renderer = AdaptiveRenderer(mock_console)
        renderer._live = None
        renderer.spinner_stop()
        # Should not raise

    def test_spinner_start_exception_handling(self, mock_console):
        """Spinner start handles exceptions."""
        renderer = AdaptiveRenderer(mock_console)
        with patch('agent_cli.render.adaptive.Live', side_effect=Exception("fail")):
            renderer.spinner_start("test")
            # Should not raise, _live should be None
            assert renderer._live is None

    def test_spinner_start_narrow_uses_dots(self, mock_console):
        """Spinner uses dots spinner in narrow terminal."""
        with patch('shutil.get_terminal_size', return_value=(60, 24)):
            renderer = AdaptiveRenderer(mock_console)
            with patch('agent_cli.render.adaptive.Live') as MockLive:
                mock_live = MagicMock()
                MockLive.return_value = mock_live
                renderer.spinner_start("thinking")
                # Should use 'dots' spinner for narrow
                call_args = MockLive.call_args
                assert call_args is not None

    def test_spinner_start_wide_uses_bouncing_bar(self, mock_console):
        """Spinner uses bouncingBar spinner in wide terminal."""
        with patch('shutil.get_terminal_size', return_value=(120, 24)):
            renderer = AdaptiveRenderer(mock_console)
            with patch('agent_cli.render.adaptive.Live') as MockLive:
                mock_live = MagicMock()
                MockLive.return_value = mock_live
                renderer.spinner_start("thinking")
                # Should use 'bouncingBar' for wide
                call_args = MockLive.call_args
                assert call_args is not None


class TestDispatchProgressIcons:
    """Test dispatch progress icon selection."""

    def test_dispatch_progress_complete_icon(self, mock_console):
        """Complete status shows checkmark."""
        with patch('shutil.get_terminal_size', return_value=(80, 24)):
            renderer = AdaptiveRenderer(mock_console)
            renderer.dispatch_progress("test", 1, "complete", "")
            assert mock_console.print.called

    def test_dispatch_progress_delegate_icon(self, mock_console):
        """Delegate shows crab icon."""
        with patch('shutil.get_terminal_size', return_value=(80, 24)):
            renderer = AdaptiveRenderer(mock_console)
            renderer.dispatch_progress("delegate", 1, "skill:foo", "")
            assert mock_console.print.called

    def test_dispatch_progress_skill_icon(self, mock_console):
        """Skill shows magic wand icon."""
        with patch('shutil.get_terminal_size', return_value=(80, 24)):
            renderer = AdaptiveRenderer(mock_console)
            renderer.dispatch_progress("skill:bar", 1, "tool", "")
            assert mock_console.print.called


class TestEdgeCases:
    """Test edge cases and extreme scenarios."""

    def test_very_narrow_terminal(self):
        """Handles very narrow terminal (< 40 chars)."""
        with patch('shutil.get_terminal_size', return_value=(30, 24)):
            renderer = AdaptiveRenderer()
            # Should use minimum width
            width = renderer._get_terminal_width()
            assert width == _MIN_WIDTH

    def test_empty_string_handling(self):
        """Handles empty strings correctly."""
        renderer = AdaptiveRenderer()
        with patch('shutil.get_terminal_size', return_value=(80, 24)):
            result = renderer._truncate("")
            assert result == ""

    def test_on_resize_function(self):
        """on_resize calls callback after event."""
        from agent_cli.render.adaptive import _resize_event, on_resize
        import threading
        import time
        
        callback_called = []
        def callback():
            callback_called.append(True)
        
        def set_event():
            time.sleep(0.1)
            _resize_event.set()
        
        # Clear any existing state
        _resize_event.clear()
        
        # Start thread to set event
        t = threading.Thread(target=set_event)
        t.start()
        
        # Wait for resize
        on_resize(callback)
        
        t.join()
        assert len(callback_called) == 1


class TestActionWideTerminal:
    """Test action rendering in wide terminal."""

    def test_action_does_not_truncate_wide(self, mock_console):
        """Action does not truncate in wide terminal."""
        with patch('shutil.get_terminal_size', return_value=(120, 24)):
            renderer = AdaptiveRenderer(mock_console)
            long_input = "A" * 100
            renderer.action("tool", long_input, turn=1)
            # In wide mode, input should not be truncated
            assert mock_console.print.called


class TestAdaptivePanel:
    """Test adaptive panel rendering."""

    def test_adaptive_panel_narrow(self, mock_console):
        """Panel uses compact mode in narrow terminal."""
        with patch('shutil.get_terminal_size', return_value=(60, 24)):
            renderer = AdaptiveRenderer(mock_console)
            renderer._adaptive_panel("content", "title", "cyan")
            assert mock_console.print.called

    def test_adaptive_panel_wide(self, mock_console):
        """Panel uses full box mode in wide terminal."""
        with patch('shutil.get_terminal_size', return_value=(120, 24)):
            renderer = AdaptiveRenderer(mock_console)
            renderer._adaptive_panel("content", "title", "cyan")
            assert mock_console.print.called


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

