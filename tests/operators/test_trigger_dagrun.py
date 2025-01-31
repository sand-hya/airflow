#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
from __future__ import annotations

import pathlib
import tempfile
from datetime import datetime
from unittest import mock

import pytest

from airflow.exceptions import AirflowException, DagRunAlreadyExists
from airflow.models.dag import DAG, DagModel
from airflow.models.dagbag import DagBag
from airflow.models.dagrun import DagRun
from airflow.models.log import Log
from airflow.models.serialized_dag import SerializedDagModel
from airflow.models.taskinstance import TaskInstance
from airflow.operators.trigger_dagrun import TriggerDagRunOperator
from airflow.triggers.external_task import DagStateTrigger
from airflow.utils import timezone
from airflow.utils.session import create_session
from airflow.utils.state import State
from airflow.utils.types import DagRunType

pytestmark = pytest.mark.db_test

DEFAULT_DATE = datetime(2019, 1, 1, tzinfo=timezone.utc)
TEST_DAG_ID = "testdag"
TRIGGERED_DAG_ID = "triggerdag"
DAG_SCRIPT = f"""\
from datetime import datetime
from airflow.models import DAG
from airflow.operators.empty import EmptyOperator

dag = DAG(
    dag_id='{TRIGGERED_DAG_ID}',
    default_args={{'start_date': datetime(2019, 1, 1)}},
    schedule_interval=None
)

task = EmptyOperator(task_id='test', dag=dag)
"""


class TestDagRunOperator:
    def setup_method(self):
        # Airflow relies on reading the DAG from disk when triggering it.
        # Therefore write a temp file holding the DAG to trigger.
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
            self._tmpfile = f.name
            f.write(DAG_SCRIPT)
            f.flush()

        with create_session() as session:
            session.add(DagModel(dag_id=TRIGGERED_DAG_ID, fileloc=self._tmpfile))
            session.commit()

        self.dag = DAG(TEST_DAG_ID, default_args={"owner": "airflow", "start_date": DEFAULT_DATE})
        dagbag = DagBag(f.name, read_dags_from_db=False, include_examples=False)
        dagbag.bag_dag(self.dag, root_dag=self.dag)
        dagbag.sync_to_db()

    def teardown_method(self):
        """Cleanup state after testing in DB."""
        with create_session() as session:
            session.query(Log).filter(Log.dag_id == TEST_DAG_ID).delete(synchronize_session=False)
            for dbmodel in [DagModel, DagRun, TaskInstance, SerializedDagModel]:
                session.query(dbmodel).filter(dbmodel.dag_id.in_([TRIGGERED_DAG_ID, TEST_DAG_ID])).delete(
                    synchronize_session=False
                )

        pathlib.Path(self._tmpfile).unlink()

    def assert_extra_link(self, triggered_dag_run, triggering_task, session):
        """
        Asserts whether the correct extra links url will be created.

        Specifically it tests whether the correct dag id and run id are passed to
        the method which constructs the final url.
        Note: We can't run that method to generate the url itself because the Flask app context
        isn't available within the test logic, so it is mocked here.
        """
        triggering_ti = (
            session.query(TaskInstance)
            .filter_by(
                task_id=triggering_task.task_id,
                dag_id=triggering_task.dag_id,
            )
            .one()
        )
        with mock.patch("airflow.operators.trigger_dagrun.build_airflow_url_with_query") as mock_build_url:
            triggering_task.get_extra_links(triggering_ti, "Triggered DAG")
        assert mock_build_url.called
        args, _ = mock_build_url.call_args
        expected_args = {
            "dag_id": triggered_dag_run.dag_id,
            "dag_run_id": triggered_dag_run.run_id,
        }
        assert expected_args in args

    def test_trigger_dagrun(self):
        """Test TriggerDagRunOperator."""
        task = TriggerDagRunOperator(task_id="test_task", trigger_dag_id=TRIGGERED_DAG_ID, dag=self.dag)
        task.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE, ignore_ti_state=True)

        with create_session() as session:
            dagrun = session.query(DagRun).filter(DagRun.dag_id == TRIGGERED_DAG_ID).one()
            assert dagrun.external_trigger
            assert dagrun.run_id == DagRun.generate_run_id(DagRunType.MANUAL, dagrun.logical_date)
            self.assert_extra_link(dagrun, task, session)

    def test_trigger_dagrun_custom_run_id(self):
        task = TriggerDagRunOperator(
            task_id="test_task",
            trigger_dag_id=TRIGGERED_DAG_ID,
            trigger_run_id="custom_run_id",
            dag=self.dag,
        )
        task.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE)

        with create_session() as session:
            dagruns = session.query(DagRun).filter(DagRun.dag_id == TRIGGERED_DAG_ID).all()
            assert len(dagruns) == 1
            assert dagruns[0].run_id == "custom_run_id"

    def test_trigger_dagrun_with_logical_date(self):
        """Test TriggerDagRunOperator with custom logical_date."""
        custom_logical_date = timezone.datetime(2021, 1, 2, 3, 4, 5)
        task = TriggerDagRunOperator(
            task_id="test_trigger_dagrun_with_logical_date",
            trigger_dag_id=TRIGGERED_DAG_ID,
            logical_date=custom_logical_date,
            dag=self.dag,
        )
        task.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE, ignore_ti_state=True)

        with create_session() as session:
            dagrun = session.query(DagRun).filter(DagRun.dag_id == TRIGGERED_DAG_ID).one()
            assert dagrun.external_trigger
            assert dagrun.logical_date == custom_logical_date
            assert dagrun.run_id == DagRun.generate_run_id(DagRunType.MANUAL, custom_logical_date)
            self.assert_extra_link(dagrun, task, session)

    def test_trigger_dagrun_twice(self):
        """Test TriggerDagRunOperator with custom logical_date."""
        utc_now = timezone.utcnow()
        task = TriggerDagRunOperator(
            task_id="test_trigger_dagrun_with_logical_date",
            trigger_dag_id=TRIGGERED_DAG_ID,
            logical_date=utc_now,
            dag=self.dag,
            poke_interval=1,
            reset_dag_run=True,
            wait_for_completion=True,
        )
        run_id = f"manual__{utc_now.isoformat()}"
        with create_session() as session:
            dag_run = DagRun(
                dag_id=TRIGGERED_DAG_ID,
                execution_date=utc_now,
                state=State.SUCCESS,
                run_type="manual",
                run_id=run_id,
            )
            session.add(dag_run)
            session.commit()
            task.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE, ignore_ti_state=True)

            dagruns = session.query(DagRun).filter(DagRun.dag_id == TRIGGERED_DAG_ID).all()
            assert len(dagruns) == 1
            triggered_dag_run = dagruns[0]
            assert triggered_dag_run.external_trigger
            assert triggered_dag_run.logical_date == utc_now
            self.assert_extra_link(triggered_dag_run, task, session)

    def test_trigger_dagrun_with_scheduled_dag_run(self):
        """Test TriggerDagRunOperator with custom logical_date and scheduled dag_run."""
        utc_now = timezone.utcnow()
        task = TriggerDagRunOperator(
            task_id="test_trigger_dagrun_with_logical_date",
            trigger_dag_id=TRIGGERED_DAG_ID,
            logical_date=utc_now,
            dag=self.dag,
            poke_interval=1,
            reset_dag_run=True,
            wait_for_completion=True,
        )
        run_id = f"scheduled__{utc_now.isoformat()}"
        with create_session() as session:
            dag_run = DagRun(
                dag_id=TRIGGERED_DAG_ID,
                execution_date=utc_now,
                state=State.SUCCESS,
                run_type="scheduled",
                run_id=run_id,
            )
            session.add(dag_run)
            session.commit()
            task.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE, ignore_ti_state=True)

            dagruns = session.query(DagRun).filter(DagRun.dag_id == TRIGGERED_DAG_ID).all()
            assert len(dagruns) == 1
            triggered_dag_run = dagruns[0]
            assert triggered_dag_run.external_trigger
            assert triggered_dag_run.logical_date == utc_now
            self.assert_extra_link(triggered_dag_run, task, session)

    def test_trigger_dagrun_with_templated_logical_date(self):
        """Test TriggerDagRunOperator with templated logical_date."""
        task = TriggerDagRunOperator(
            task_id="test_trigger_dagrun_with_str_logical_date",
            trigger_dag_id=TRIGGERED_DAG_ID,
            logical_date="{{ logical_date }}",
            dag=self.dag,
        )
        task.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE, ignore_ti_state=True)

        with create_session() as session:
            dagruns = session.query(DagRun).filter(DagRun.dag_id == TRIGGERED_DAG_ID).all()
            assert len(dagruns) == 1
            triggered_dag_run = dagruns[0]
            assert triggered_dag_run.external_trigger
            assert triggered_dag_run.logical_date == DEFAULT_DATE
            self.assert_extra_link(triggered_dag_run, task, session)

    def test_trigger_dagrun_operator_conf(self):
        """Test passing conf to the triggered DagRun."""
        task = TriggerDagRunOperator(
            task_id="test_trigger_dagrun_with_str_logical_date",
            trigger_dag_id=TRIGGERED_DAG_ID,
            conf={"foo": "bar"},
            dag=self.dag,
        )
        task.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE, ignore_ti_state=True)

        with create_session() as session:
            dagruns = session.query(DagRun).filter(DagRun.dag_id == TRIGGERED_DAG_ID).all()
            assert len(dagruns) == 1
            assert dagruns[0].conf == {"foo": "bar"}

    def test_trigger_dagrun_operator_templated_invalid_conf(self):
        """Test passing a conf that is not JSON Serializable raise error."""
        task = TriggerDagRunOperator(
            task_id="test_trigger_dagrun_with_invalid_conf",
            trigger_dag_id=TRIGGERED_DAG_ID,
            conf={"foo": "{{ dag.dag_id }}", "datetime": timezone.utcnow()},
            dag=self.dag,
        )
        with pytest.raises(AirflowException, match="^conf parameter should be JSON Serializable$"):
            task.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE)

    def test_trigger_dagrun_operator_templated_conf(self):
        """Test passing a templated conf to the triggered DagRun."""
        task = TriggerDagRunOperator(
            task_id="test_trigger_dagrun_with_str_logical_date",
            trigger_dag_id=TRIGGERED_DAG_ID,
            conf={"foo": "{{ dag.dag_id }}"},
            dag=self.dag,
        )
        task.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE, ignore_ti_state=True)

        with create_session() as session:
            dagruns = session.query(DagRun).filter(DagRun.dag_id == TRIGGERED_DAG_ID).all()
            assert len(dagruns) == 1
            assert dagruns[0].conf == {"foo": TEST_DAG_ID}

    def test_trigger_dagrun_with_reset_dag_run_false(self):
        """Test TriggerDagRunOperator without reset_dag_run."""
        logical_date = DEFAULT_DATE
        task = TriggerDagRunOperator(
            task_id="test_task",
            trigger_dag_id=TRIGGERED_DAG_ID,
            trigger_run_id=None,
            logical_date=None,
            reset_dag_run=False,
            dag=self.dag,
        )
        task.run(start_date=logical_date, end_date=logical_date, ignore_ti_state=True)
        task.run(start_date=logical_date, end_date=logical_date, ignore_ti_state=True)

        with create_session() as session:
            dagruns = session.query(DagRun).filter(DagRun.dag_id == TRIGGERED_DAG_ID).all()
            assert len(dagruns) == 2

    @pytest.mark.parametrize(
        "trigger_run_id, trigger_logical_date",
        [
            (None, DEFAULT_DATE),
            ("dummy_run_id", None),
            ("dummy_run_id", DEFAULT_DATE),
        ],
    )
    def test_trigger_dagrun_with_reset_dag_run_false_fail(self, trigger_run_id, trigger_logical_date):
        """Test TriggerDagRunOperator without reset_dag_run but triggered dag fails."""
        logical_date = DEFAULT_DATE
        task = TriggerDagRunOperator(
            task_id="test_task",
            trigger_dag_id=TRIGGERED_DAG_ID,
            trigger_run_id=trigger_run_id,
            logical_date=trigger_logical_date,
            reset_dag_run=False,
            dag=self.dag,
        )
        task.run(start_date=logical_date, end_date=logical_date, ignore_ti_state=True)

        with pytest.raises(DagRunAlreadyExists):
            task.run(start_date=logical_date, end_date=logical_date, ignore_ti_state=True)

    @pytest.mark.parametrize(
        "trigger_run_id, trigger_logical_date, expected_dagruns_count",
        [
            (None, DEFAULT_DATE, 1),
            (None, None, 2),
            ("dummy_run_id", DEFAULT_DATE, 1),
            ("dummy_run_id", None, 1),
        ],
    )
    def test_trigger_dagrun_with_reset_dag_run_true(
        self, trigger_run_id, trigger_logical_date, expected_dagruns_count
    ):
        """Test TriggerDagRunOperator with reset_dag_run."""
        logical_date = DEFAULT_DATE
        task = TriggerDagRunOperator(
            task_id="test_task",
            trigger_dag_id=TRIGGERED_DAG_ID,
            trigger_run_id=trigger_run_id,
            logical_date=trigger_logical_date,
            reset_dag_run=True,
            dag=self.dag,
        )
        task.run(start_date=logical_date, end_date=logical_date, ignore_ti_state=True)
        task.run(start_date=logical_date, end_date=logical_date, ignore_ti_state=True)

        with create_session() as session:
            dag_runs = session.query(DagRun).filter(DagRun.dag_id == TRIGGERED_DAG_ID).all()
            assert len(dag_runs) == expected_dagruns_count
            assert dag_runs[0].external_trigger

    def test_trigger_dagrun_with_wait_for_completion_true(self):
        """Test TriggerDagRunOperator with wait_for_completion."""
        logical_date = DEFAULT_DATE
        task = TriggerDagRunOperator(
            task_id="test_task",
            trigger_dag_id=TRIGGERED_DAG_ID,
            logical_date=logical_date,
            wait_for_completion=True,
            poke_interval=10,
            allowed_states=[State.QUEUED],
            dag=self.dag,
        )
        task.run(start_date=logical_date, end_date=logical_date)

        with create_session() as session:
            dagruns = session.query(DagRun).filter(DagRun.dag_id == TRIGGERED_DAG_ID).all()
            assert len(dagruns) == 1

    def test_trigger_dagrun_with_wait_for_completion_true_fail(self):
        """Test TriggerDagRunOperator with wait_for_completion but triggered dag fails."""
        logical_date = DEFAULT_DATE
        task = TriggerDagRunOperator(
            task_id="test_task",
            trigger_dag_id=TRIGGERED_DAG_ID,
            logical_date=logical_date,
            wait_for_completion=True,
            poke_interval=10,
            failed_states=[State.QUEUED],
            dag=self.dag,
        )
        with pytest.raises(AirflowException):
            task.run(start_date=logical_date, end_date=logical_date)

    def test_trigger_dagrun_triggering_itself(self):
        """Test TriggerDagRunOperator that triggers itself"""
        logical_date = DEFAULT_DATE
        task = TriggerDagRunOperator(
            task_id="test_task",
            trigger_dag_id=self.dag.dag_id,
            dag=self.dag,
        )
        task.run(start_date=logical_date, end_date=logical_date)

        with create_session() as session:
            dagruns = (
                session.query(DagRun)
                .filter(DagRun.dag_id == self.dag.dag_id)
                .order_by(DagRun.execution_date)
                .all()
            )
            assert len(dagruns) == 2
            triggered_dag_run = dagruns[1]
            assert triggered_dag_run.state == State.QUEUED
            self.assert_extra_link(triggered_dag_run, task, session)

    def test_trigger_dagrun_triggering_itself_with_logical_date(self):
        """Test TriggerDagRunOperator that triggers itself with logical date,
        fails with DagRunAlreadyExists"""
        logical_date = DEFAULT_DATE
        task = TriggerDagRunOperator(
            task_id="test_task",
            trigger_dag_id=self.dag.dag_id,
            logical_date=logical_date,
            dag=self.dag,
        )
        with pytest.raises(DagRunAlreadyExists):
            task.run(start_date=logical_date, end_date=logical_date)

    def test_trigger_dagrun_with_wait_for_completion_true_defer_false(self):
        """Test TriggerDagRunOperator with wait_for_completion."""
        logical_date = DEFAULT_DATE
        task = TriggerDagRunOperator(
            task_id="test_task",
            trigger_dag_id=TRIGGERED_DAG_ID,
            logical_date=logical_date,
            wait_for_completion=True,
            poke_interval=10,
            allowed_states=[State.QUEUED],
            deferrable=False,
            dag=self.dag,
        )
        task.run(start_date=logical_date, end_date=logical_date)

        with create_session() as session:
            dagruns = session.query(DagRun).filter(DagRun.dag_id == TRIGGERED_DAG_ID).all()
            assert len(dagruns) == 1

    def test_trigger_dagrun_with_wait_for_completion_true_defer_true(self):
        """Test TriggerDagRunOperator with wait_for_completion."""
        logical_date = DEFAULT_DATE
        task = TriggerDagRunOperator(
            task_id="test_task",
            trigger_dag_id=TRIGGERED_DAG_ID,
            logical_date=logical_date,
            wait_for_completion=True,
            poke_interval=10,
            allowed_states=[State.QUEUED],
            deferrable=True,
            dag=self.dag,
        )

        task.run(start_date=logical_date, end_date=logical_date)

        with create_session() as session:
            dagruns = session.query(DagRun).filter(DagRun.dag_id == TRIGGERED_DAG_ID).all()
            assert len(dagruns) == 1
        trigger = DagStateTrigger(
            dag_id="down_stream",
            execution_dates=[DEFAULT_DATE],
            poll_interval=20,
            states=["success", "failed"],
        )

        task.execute_complete(context={}, event=trigger.serialize())

    def test_trigger_dagrun_with_wait_for_completion_true_defer_true_failure(self):
        """Test TriggerDagRunOperator wait_for_completion dag run in non defined state."""
        logical_date = DEFAULT_DATE
        task = TriggerDagRunOperator(
            task_id="test_task",
            trigger_dag_id=TRIGGERED_DAG_ID,
            logical_date=logical_date,
            wait_for_completion=True,
            poke_interval=10,
            allowed_states=[State.SUCCESS],
            deferrable=True,
            dag=self.dag,
        )

        task.run(start_date=logical_date, end_date=logical_date)

        with create_session() as session:
            dagruns = session.query(DagRun).filter(DagRun.dag_id == TRIGGERED_DAG_ID).all()
            assert len(dagruns) == 1

        trigger = DagStateTrigger(
            dag_id="down_stream",
            execution_dates=[DEFAULT_DATE],
            poll_interval=20,
            states=["success", "failed"],
        )
        with pytest.raises(AirflowException, match="which is not in"):
            task.execute_complete(
                context={},
                event=trigger.serialize(),
            )

    def test_trigger_dagrun_with_wait_for_completion_true_defer_true_failure_2(self):
        """Test TriggerDagRunOperator  wait_for_completion dag run in failed state."""
        logical_date = DEFAULT_DATE
        task = TriggerDagRunOperator(
            task_id="test_task",
            trigger_dag_id=TRIGGERED_DAG_ID,
            logical_date=logical_date,
            wait_for_completion=True,
            poke_interval=10,
            allowed_states=[State.SUCCESS],
            failed_states=[State.QUEUED],
            deferrable=True,
            dag=self.dag,
        )

        task.run(start_date=logical_date, end_date=logical_date)

        with create_session() as session:
            dagruns = session.query(DagRun).filter(DagRun.dag_id == TRIGGERED_DAG_ID).all()
            assert len(dagruns) == 1

        trigger = DagStateTrigger(
            dag_id="down_stream",
            execution_dates=[DEFAULT_DATE],
            poll_interval=20,
            states=["success", "failed"],
        )

        with pytest.raises(AirflowException, match="failed with failed state"):
            task.execute_complete(context={}, event=trigger.serialize())

    def test_trigger_dagrun_with_execution_date(self):
        """Test TriggerDagRunOperator with custom execution_date (deprecated parameter)"""
        custom_execution_date = timezone.datetime(2021, 1, 2, 3, 4, 5)
        task = TriggerDagRunOperator(
            task_id="test_trigger_dagrun_with_execution_date",
            trigger_dag_id=TRIGGERED_DAG_ID,
            execution_date=custom_execution_date,
            dag=self.dag,
        )
        task.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE, ignore_ti_state=True)

        with create_session() as session:
            dagrun = session.query(DagRun).filter(DagRun.dag_id == TRIGGERED_DAG_ID).one()
            assert dagrun.external_trigger
            assert dagrun.logical_date == custom_execution_date
            assert dagrun.run_id == DagRun.generate_run_id(DagRunType.MANUAL, custom_execution_date)
            self.assert_extra_link(dagrun, task, session)
