from __future__ import annotations

import copy
import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import scidev.agent.core
import scidev.agent.multi_agent
from scidev.agent.core import CodingAgent
from scidev.agent.research import delegate_parallel_research, delegate_research


class ResearchBoundaryTests(unittest.TestCase):
    def _dependencies(self):
        root = Path.cwd()
        trace = []
        saved = []

        def save(session):
            saved.append(copy.deepcopy(session))
            trace.append((threading.get_ident(), 'save'))

        def record(event, payload):
            trace.append((threading.get_ident(), event))

        dependencies = {
            'project_root': root,
            'toolbox': SimpleNamespace(project_context=SimpleNamespace(resolve_scope=lambda path: root / path)),
            'default_model': 'configured-specialist',
            'call_limit': 3,
            'ledger': SimpleNamespace(append=record),
            'snapshot': lambda session: {'complete': True, 'fingerprint': 'workspace-version'},
            'request_scope_id': lambda session_id, task_id: f'{session_id}-{task_id}',
            'save': save,
            'emit': lambda name, payload: None,
            'record_model_request': lambda session, key, phase, metrics: trace.append((threading.get_ident(), phase)),
        }
        return dependencies, trace, saved

    def test_research_import_does_not_load_agent_model_or_ui(self):
        script = (
            'import sys; import scidev.agent.research; '
            'assert not any(name in sys.modules for name in '
            '("scidev.agent.core", "scidev.desktop.client", "scidev.integrations.model", "scidev.agent.toolbox")); '
            'assert not any(name.startswith("PySide6") for name in sys.modules)'
        )
        result = subprocess.run(
            [sys.executable, '-B', '-c', script],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'},
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_legacy_specialist_exports_keep_object_identity(self):
        for name in ('run_specialist_agent', 'validate_delegation', 'validate_parallel_delegations'):
            with self.subTest(name=name):
                self.assertIs(getattr(scidev.agent.core, name), getattr(scidev.agent.multi_agent, name))

    def test_agent_adapters_bind_current_dependencies_without_initialization(self):
        agent = CodingAgent.__new__(CodingAgent)
        dependencies, _, _ = self._dependencies()
        agent.project_root = dependencies['project_root']
        agent.toolbox = dependencies['toolbox']
        agent.ledger = dependencies['ledger']
        agent.summary_settings = SimpleNamespace(multi_agent_model='first-model')
        agent.MAX_SPECIALIST_CALLS_PER_TASK = 1
        agent._verification_snapshot = dependencies['snapshot']
        agent._request_scope_id = dependencies['request_scope_id']
        agent._save_session = dependencies['save']
        agent._emit = dependencies['emit']
        agent._record_model_request = dependencies['record_model_request']
        for method, target in (
            (agent._delegate_research, 'scidev.agent.research.delegate_research'),
            (agent._delegate_parallel_research, 'scidev.agent.research.delegate_parallel_research'),
        ):
            with self.subTest(target=target), patch(target, return_value='result') as delegate:
                for index in (1, 2):
                    def callback(session):
                        pass
                    agent._save_session = callback
                    agent.summary_settings.multi_agent_model = f'model-{index}'
                    agent.MAX_SPECIALIST_CALLS_PER_TASK = index
                    session = {'session_id': 's'}
                    result = method('provider', session, 'prompt', {}, turn=1, workspace_revision=2, task_id='t')
                    self.assertEqual(result, 'result')
                    arguments = delegate.call_args.kwargs
                    self.assertIs(arguments['save'], callback)
                    self.assertIs(arguments['snapshot'], agent._verification_snapshot)
                    self.assertIs(arguments['emit'], agent._emit)
                    self.assertIs(arguments['record_model_request'], agent._record_model_request)
                    self.assertEqual(arguments['call_limit'], index)
                    self.assertEqual(arguments['default_model'], f'model-{index}')

    def test_initial_save_failure_does_not_start_single_or_parallel_work(self):
        requests = [
            {'role': 'tests', 'question': 'Find parser tests', 'focus_paths': ['tests']},
            {'role': 'architecture', 'question': 'Find parser entry', 'focus_paths': ['src']},
        ]
        for delegate, arguments in (
            (delegate_research, requests[0]),
            (delegate_parallel_research, {'delegations': requests}),
        ):
            with self.subTest(delegate=delegate.__name__):
                dependencies, trace, _ = self._dependencies()
                def fail_save(session):
                    raise OSError('save failed')

                dependencies['save'] = fail_save
                with patch('scidev.agent.research.run_specialist_agent') as run, self.assertRaisesRegex(OSError, 'save failed'):
                    delegate(
                        object(), {'session_id': 's'}, 'Inspect parser', arguments,
                        turn=1, workspace_revision=0, task_id='t', **dependencies,
                    )
                run.assert_not_called()
                self.assertEqual(trace, [])

    def test_parallel_accounting_and_saves_stay_on_the_calling_thread(self):
        dependencies, trace, saved = self._dependencies()
        calling_thread = threading.get_ident()
        worker_threads = []
        barrier = threading.Barrier(2)

        def specialist(provider, toolbox, **kwargs):
            worker_threads.append(threading.get_ident())
            barrier.wait(timeout=5)
            kwargs['on_event']('specialist_tool_called', {'tool': 'read_file'})
            kwargs['on_metrics'](1, {'total_tokens': 12})
            return {'status': 'completed', 'summary': kwargs['role'], 'turns': 1, 'tool_calls': 1}

        session = {'session_id': 's'}
        with patch('scidev.agent.research.run_specialist_agent', side_effect=specialist):
            result = delegate_parallel_research(
                object(), session, 'Inspect parser', {'delegations': [
                    {'role': 'tests', 'question': 'Find tests'},
                    {'role': 'architecture', 'question': 'Find entry'},
                ]}, turn=1, workspace_revision=0, task_id='t', **dependencies,
            )
        self.assertEqual(len(set(worker_threads)), 2)
        self.assertNotIn(calling_thread, worker_threads)
        self.assertTrue(all(thread_id == calling_thread for thread_id, _ in trace))
        self.assertEqual([record['status'] for record in saved[0]['specialist_calls']], ['running', 'running'])
        self.assertEqual([record['status'] for record in saved[-1]['specialist_calls']], ['completed', 'completed'])
        self.assertEqual([name for _, name in trace].count('specialist'), 2)
        self.assertEqual(len(saved), 3)
        self.assertLess(result.index('tests'), result.index('architecture'))


if __name__ == '__main__':
    unittest.main()
