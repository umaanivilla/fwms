# FWMS task engine: a self-assignment queue — workers claim work from live
# floor state instead of being centrally dispatched.

from __future__ import annotations

import frappe
from frappe.utils import now

from fwms.events import emit

# categories used by the task engine when tasks change state
TASK_CATEGORY = {
	"Receive": "Receiving & QC",
	"Putaway": "Putaway & Storage",
	"Pick": "Orders & Picking",
	"Replenish": "Putaway & Storage",
	"Count": "Inventory Control",
	"Pack": "Packing",
	"Load": "Sort Load & Ship",
}

PRIORITY_ORDER = frappe._dict({"1-High": 1, "2-Normal": 2, "3-Low": 3})


def create_task(
	task_type: str,
	priority: str = "2-Normal",
	references: list[dict] | None = None,
	emit_event: bool = True,
	**fields,
) -> str:
	"""Create an Open task. `fields` map to FWMS Task columns (item, qty, lpn, from_bin, to_bin, wave...)."""
	doc = frappe.get_doc({
		"doctype": "FWMS Task",
		"task_type": task_type,
		"priority": priority,
		"status": "Open",
		**fields,
	})
	for ref in references or []:
		doc.append("references", ref)
	doc.insert(ignore_permissions=True)

	if emit_event:
		emit(
			TASK_CATEGORY.get(task_type, "Alerts & Supervisor"),
			f"{task_type} task {doc.name} queued",
			subtype="task_created",
			reference_doctype="FWMS Task",
			reference_name=doc.name,
			payload={"task_type": task_type, "item": fields.get("item"), "qty": fields.get("qty")},
		)
	return doc.name


def _worker_skills(worker: str) -> set[str]:
	skills = frappe.db.get_value("FWMS Worker", worker, "skills") or ""
	return {s.strip() for s in skills.split(",") if s.strip()}


def claim_next(worker: str, task_type: str | None = None) -> str | None:
	"""Self-assignment: claim the highest-priority oldest Open task the worker can do."""
	if frappe.db.get_value("FWMS Worker", worker, "current_task"):
		frappe.throw(f"Worker {worker} already has an assigned task")

	skills = _worker_skills(worker)
	default_zone = frappe.db.get_value("FWMS Worker", worker, "default_zone")

	filters = {"status": "Open"}
	if task_type:
		filters["task_type"] = task_type
	tasks = frappe.get_all(
		"FWMS Task",
		filters=filters,
		fields=["name", "task_type", "priority", "to_bin"],
		order_by="priority asc, creation asc",
		limit=100,
	)
	if not tasks:
		return None

	for task in tasks:
		if skills and task.task_type not in skills:
			continue
		frappe.db.set_value("FWMS Task", task.name, {
			"status": "Assigned",
			"worker": worker,
			"assigned_at": now(),
		}, update_modified=True)
		frappe.db.set_value("FWMS Worker", worker, "current_task", task.name, update_modified=False)
		emit(
			TASK_CATEGORY.get(task.task_type, "Alerts & Supervisor"),
			f"Worker {worker} claimed {task.task_type} task {task.name}",
			subtype="task_claimed",
			worker=worker,
			reference_doctype="FWMS Task",
			reference_name=task.name,
		)
		return task.name
	return None


def start_task(task_name: str, worker: str | None = None) -> None:
	patch = {"status": "In Progress", "started_at": now()}
	if worker:
		patch["worker"] = worker
	frappe.db.set_value("FWMS Task", task_name, patch, update_modified=True)


def complete_task(task_name: str, result_note: str | None = None) -> None:
	task = frappe.db.get_value("FWMS Task", task_name, ["worker", "task_type"], as_dict=True)
	frappe.db.set_value("FWMS Task", task_name, {
		"status": "Complete",
		"completed_at": now(),
		"result_note": result_note,
	}, update_modified=True)
	if task and task.worker:
		frappe.db.set_value("FWMS Worker", task.worker, {
			"current_task": None,
			"tasks_completed": (frappe.db.get_value("FWMS Worker", task.worker, "tasks_completed") or 0) + 1,
		}, update_modified=False)
	emit(
		TASK_CATEGORY.get(task.task_type if task else "", "Alerts & Supervisor"),
		f"Task {task_name} complete",
		subtype="task_complete",
		worker=task.worker if task else None,
		reference_doctype="FWMS Task",
		reference_name=task_name,
	)


def cancel_task(task_name: str, reason: str | None = None) -> None:
	frappe.db.set_value("FWMS Task", task_name, {
		"status": "Cancelled",
		"completed_at": now(),
		"result_note": reason,
	}, update_modified=True)


def assign_to_next_worker(doctype: str | None = None, name: str | None = None, doc=None) -> None:
	"""Server action on FWMS Task list: return tasks to the Open queue."""
	targets = [name] if name else []
	if doc:
		targets.append(doc.name)
	for task_name in targets:
		if not task_name:
			continue
		worker = frappe.db.get_value("FWMS Task", task_name, "worker")
		frappe.db.set_value("FWMS Task", task_name, {
			"status": "Open",
			"worker": None,
			"assigned_at": None,
		}, update_modified=True)
		if worker:
			frappe.db.set_value("FWMS Worker", worker, "current_task", None, update_modified=False)
