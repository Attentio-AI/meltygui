import contextlib
import io
import sys
import threading
import unittest

from meltygui.models.function_console import FunctionConsole
from meltygui.models.function_console import capture


class ConsoleTests(unittest.TestCase):
    def test_interactive_input_output_and_eof(self):
        wake = threading.Event()
        console = FunctionConsole(wake.set)
        result = []
        def run():
            print('hello', file=sys.stderr)
            name = input('Name: ')
            print('Welcome', name)
            print('rest:', repr(sys.stdin.read()))
            return True, None
        original = sys.stdin, sys.stdout, sys.stderr
        self.assertTrue(console.start(run, result.append))
        self.assertFalse(console.start(run, result.append))
        for _ in range(100):
            if console.waiting:
                break
            wake.wait(.05)
            wake.clear()
        self.assertTrue(console.waiting)
        self.assertIn('Name: ', console.text)
        console.send('Lukas')
        console.close_input()
        console.thread.join(2)
        self.assertFalse(console.thread.is_alive())
        self.assertEqual(result, [(True, None)])
        self.assertIn('hello\nName: Lukas\nWelcome Lukas\nrest: \'\'\n', console.text)
        self.assertEqual((sys.stdin, sys.stdout, sys.stderr), original)

    def test_thread_isolation_and_concurrent_capture(self):
        other = io.StringIO()
        first, second = FunctionConsole(), FunctionConsole()
        barrier = threading.Barrier(3)
        def run(console, label):
            with capture(console):
                barrier.wait(2)
                print(label)
                barrier.wait(2)
        with contextlib.redirect_stdout(other):
            threads = [threading.Thread(target=run, args=(first, 'one')),
                       threading.Thread(target=run, args=(second, 'two'))]
            for thread in threads:
                thread.start()
            barrier.wait(2)
            print('editor output')
            barrier.wait(2)
            for thread in threads:
                thread.join(2)
        self.assertEqual(first.text, 'one\n')
        self.assertEqual(second.text, 'two\n')
        self.assertEqual(other.getvalue(), 'editor output\n')

    def test_partial_reads_and_eof(self):
        console = FunctionConsole()
        console.running = True
        console.send('abc')
        console.send('def')
        console.close_input()
        self.assertEqual(console.stdin.read(2), 'ab')
        self.assertEqual(console.stdin.readline(), 'c\n')
        self.assertEqual(console.stdin.readline(2), 'de')
        self.assertEqual(console.stdin.read(), 'f\n')
        self.assertEqual(console.stdin.readline(), '')

    def test_exit_and_bounded_output(self):
        console = FunctionConsole()
        result = []
        def run():
            print('x' * (console.LIMIT + 1))
            raise SystemExit(2)
        console.start(run, result.append)
        console.thread.join(2)
        self.assertFalse(console.running)
        self.assertEqual(result, [(False, 'SystemExit: 2')])
        self.assertLessEqual(len(console.text), console.LIMIT)
        self.assertIn('SystemExit: 2', console.text)


if __name__ == '__main__':
    unittest.main()
