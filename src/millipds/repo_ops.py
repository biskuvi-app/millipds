"""
Theory: all MST-wrangling should happen in here, but all SQL happens in database.py

(in the interrim we'll do raw SQL in here, and refactor later...)

actuallyyyyyyy I think I changed my mind. given the sheer volume of SQL involved, and
its tight coupling to the actual commit logic, I think it makes the most sense to have it right here.

I'm never planning on replacing sqlite with anything else, so the tight coupling is fine.
"""

import io
from typing import List, TypedDict, Literal, NotRequired
import apsw

import cbrrr

from atmst.blockstore import OverlayBlockStore, MemoryBlockStore
from atmst.mst.node_store import NodeStore
from atmst.mst.node_wrangler import NodeWrangler
from atmst.mst.node_walker import NodeWalker
from atmst.mst.diff import mst_diff, record_diff, DeltaType

from .database import Database, DBBlockStore
from . import util
from . import crypto

import logging
logger = logging.getLogger(__name__)

# https://github.com/bluesky-social/atproto/blob/main/lexicons/com/atproto/repo/applyWrites.json
WriteOp = TypedDict("WriteOp", {
	"$type": Literal["com.atproto.repo.applyWrites#create", "com.atproto.repo.applyWrites#update", "com.atproto.repo.applyWrites#delete"],
	"collection": str,
	"rkey": NotRequired[str], # required for update, delete
	"validate": NotRequired[bool],
	"swapRecord": NotRequired[str],
	"value": NotRequired[dict] # not required for delete
})

# This is perhaps the most complex function in the whole codebase.
# There's probably some scope for refactoring, but I like the "directness" of it.
# The work it does is inherently complex, i.e. the atproto MST record commit logic
# The MST logic itself is hidden away inside the `atmst` module.
def apply_writes(db: Database, repo: str, writes: List[WriteOp]):
	with db.new_con() as con: # one big transaction (we could perhaps work in two phases, prepare (via read-only conn) then commit?)
		db_bs = DBBlockStore(con, repo)
		mem_bs = MemoryBlockStore()
		bs = OverlayBlockStore(mem_bs, db_bs)
		ns = NodeStore(bs)
		wrangler = NodeWrangler(ns)
		user_id, prev_commit, signing_key_pem = con.execute(
			"SELECT id, commit_bytes, signing_key FROM user WHERE did=?",
			(repo,)
		).fetchone()
		prev_commit = cbrrr.decode_dag_cbor(prev_commit)
		prev_commit_root = prev_commit["data"]
		tid_now = util.tid_now()

		record_cbors: dict[cbrrr.CID, bytes] = {}

		# step 0: apply writes into the MST
		# TODO: should I forbid touching the same record more than once?
		prev_root = prev_commit_root
		results = [] # for result of applyWrites
		for op in writes:
			optype = op["$type"]
			# TODO: rkey validation!
			if optype in ["com.atproto.repo.applyWrites#create", "com.atproto.repo.applyWrites#update"]:
				rkey = op.get("rkey") or tid_now
				path = op["collection"] + "/" + rkey
				if optype == "com.atproto.repo.applyWrites#create":
					if NodeWalker(ns, prev_root).find_value(path):
						raise Exception("record already exists")
				value_cbor = cbrrr.encode_dag_cbor(op["value"], atjson_mode=True)
				value_cid = cbrrr.CID.cidv1_dag_cbor_sha256_32_from(value_cbor)
				record_cbors[value_cid] = value_cbor
				next_root = wrangler.put_record(prev_root, path, value_cid)
				results.append({
					"$type": optype + "Result",
					"uri": f"at://{repo}/{path}",
					"cid": value_cid.encode(),
					"validationStatus": "unknown" # we are not currently aware of the concept of a lexicon!
				})
			elif optype == "com.atproto.repo.applyWrites#delete":
				next_root = wrangler.del_record(prev_root, op["collection"] + "/" + op["rkey"])
				if prev_root == next_root:
					raise Exception("no such record") # TODO: better error signalling!!!
				results.append({
					"$type": "com.atproto.repo.applyWrites#deleteResult"
				})
			else:
				raise ValueError("invalid applyWrites type")
			prev_root = next_root
		next_commit_root = prev_root

		logger.info(f"mst root {prev_commit_root.encode()} -> {next_commit_root.encode()}")

		# step 1: diff the mst
		created, deleted = mst_diff(ns, prev_commit_root, next_commit_root)

		# step 2: persist record changes
		# (and also build ops list for firehose)
		new_record_cids = []
		firehose_ops = []
		for delta in record_diff(ns, created, deleted):
			if delta.prior_value:
				# needed for blob decref
				prior_value = con.execute(
					"SELECT value FROM record WHERE repo=? AND path=?",
					(user_id, delta.key)
				).fetchone()[0]
			if delta.delta_type == DeltaType.CREATED:
				new_record_cids.append(delta.later_value)
				firehose_ops.append({
					"cid": delta.later_value,
					"path": delta.key,
					"action": "create"
				})
				new_value = record_cbors[delta.later_value]
				blob_incref_all(con, user_id, new_value, tid_now)
				con.execute(
					"INSERT INTO record (repo, path, cid, since, value) VALUES (?, ?, ?, ?, ?)",
					(user_id, delta.key, bytes(delta.later_value), tid_now, new_value)
				)
			elif delta.delta_type == DeltaType.UPDATED:
				new_record_cids.append(delta.later_value)
				firehose_ops.append({
					"cid": delta.later_value,
					"path": delta.key,
					"action": "update"
				})
				new_value = record_cbors[delta.later_value]
				blob_incref_all(con, user_id, new_value, tid_now) # important to incref before decref
				blob_decref_all(con, user_id, prior_value)
				con.execute(
					"UPDATE record SET cid=?, since=?, value=? WHERE repo=? AND path=?",
					(bytes(delta.later_value), tid_now, new_value, user_id, delta.key)
				)
			elif delta.delta_type == DeltaType.DELETED:
				firehose_ops.append({
					"cid": None,
					"path": delta.key,
					"action": "delete"
				})
				blob_decref_all(con, user_id, prior_value)
				con.execute(
					"DELETE FROM record WHERE repo=? AND path=?",
					(user_id, delta.key)
				)
			else:
				raise Exception("unreachable")
		
		# step 3: persist MST changes (we have to do this now because record_diff might need to read some blocks)
		con.executemany(
			"DELETE FROM mst WHERE repo=? AND cid=?",
			[(user_id, cid) for cid in map(bytes, deleted)]
		)
		con.executemany(
			"INSERT INTO mst (repo, cid, since, value) VALUES (?, ?, ?, ?)",
			[(user_id, cid, tid_now, bs.get_block(cid)) for cid in map(bytes, created)]
		)
		
		# prepare the signed commit object
		commit_obj = {
			"version": 3,
			"did": repo,
			"data": next_commit_root,
			"rev": tid_now,
			"prev": None,
		}
		commit_obj["sig"] = crypto.raw_sign(
			crypto.privkey_from_pem(signing_key_pem),
			cbrrr.encode_dag_cbor(commit_obj)
		)
		commit_bytes = cbrrr.encode_dag_cbor(commit_obj)
		commit_cid = cbrrr.CID.cidv1_dag_cbor_sha256_32_from(commit_bytes)

		# persist commit object
		con.execute(
			"UPDATE user SET commit_bytes=?, head=?, rev=? WHERE did=?",
			(commit_bytes, bytes(commit_cid), tid_now, repo)
		)

		car = io.BytesIO()
		cw = util.CarWriter(car, commit_cid)
		cw.write_block(commit_cid, commit_bytes)
		for mst_cid in created:
			cw.write_block(mst_cid, bs.get_block(bytes(mst_cid)))
		for record_cid in new_record_cids:
			cw.write_block(record_cid, record_cbors[record_cid])
		
		firehose_seq = con.execute("SELECT IFNULL(MAX(seq), 0) + 1 FROM firehose").fetchone()[0]
		firehose_body = {
			"ops": firehose_ops,
			"seq": firehose_seq,
			"rev": tid_now,
			"since": prev_commit["rev"],
			"prev": None,
			"repo": repo,
			"time": util.iso_string_now(),
			"blobs": [],  # TODO!!!
			"blocks": car.getvalue(),
			"commit": commit_cid,
			"rebase": False,  # deprecated but still required
			"tooBig": False,  # TODO: actually check lol
		}
		firehose_bytes = cbrrr.encode_dag_cbor({
			"t": "#commit",
			"op": 1
		}) + cbrrr.encode_dag_cbor(firehose_body)
		con.execute(
			"INSERT INTO firehose (seq, timestamp, msg) VALUES (?, ?, ?)",
			(firehose_seq, 0, firehose_bytes) # TODO: put sensible timestamp here...
		)

		applywrites_res = {
			"commit": {
				"cid": commit_cid.encode(),
				"rev": tid_now
			},
			"results": results
		}

		return applywrites_res, firehose_bytes


# and also set `since`, if previously unset
# NB: both of these will incref/decref the same blob multiple times, if a record contains the same blob multiple times.
# this is mildly sub-optimal perf-wise but it keeps the code simple.
# (why would you reference the same blob multiple times anyway?)
def blob_incref_all(con: apsw.Connection, user_id: int, record_bytes: bytes, tid: str):
	for ref in util.enumerate_blob_cids(cbrrr.decode_dag_cbor(record_bytes)):
		blob_incref(con, user_id, ref, tid)

def blob_decref_all(con: apsw.Connection, user_id: int, record_bytes: bytes):
	for ref in util.enumerate_blob_cids(cbrrr.decode_dag_cbor(record_bytes)):
		blob_decref(con, user_id, ref)

def blob_incref(con: apsw.Connection, user_id: int, ref: cbrrr.CID, tid: str):
	# also set `since` if this is the first time a blob has ever been ref'd
	con.execute(
		"UPDATE blob SET refcount=refcount+1, since=IFNULL(since, ?) WHERE blob.repo=? AND blob.cid=?",
		(tid, user_id, bytes(ref))
	)
	changes = con.changes()  # number of updated rows

	if changes == 1:
		return  # happy path

	if changes == 0:
		raise ValueError("tried to incref a blob that doesn't exist") # could happen if e.g. user didn't upload blob first
	
	# changes > 1
	raise ValueError("welp, that's not supposed to happen") # should be impossible given UNIQUE constraints

def blob_decref(con: apsw.Connection, user_id: int, ref: cbrrr.CID):
	blob_id, refcount = con.execute(
		"UPDATE blob SET refcount=refcount-1 WHERE blob.repo=? AND blob.cid=? RETURNING id, refcount",
		(user_id, bytes(ref))
	).fetchone()

	assert(con.changes() == 1)
	assert(refcount >= 0)

	if refcount == 0:
		con.execute("DELETE FROM blob_part WHERE blob=?", (blob_id,)) # TODO: could also make this happen in a delete hook?
		con.execute("DELETE FROM blob WHERE id=?", (blob_id,))
