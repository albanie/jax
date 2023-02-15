# Copyright 2022 The JAX Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from __future__ import annotations

import dataclasses
import inspect
import threading
from typing import Any, Dict, Hashable, List, Optional, Protocol, Tuple

import numpy as np

import jax.numpy as jnp
from jax import tree_util
from jax._src import core
from jax._src import debugging
from jax._src import traceback_util
from jax._src import util


@tree_util.register_pytree_node_class
class _DictWrapper:
  keys: list[Hashable]
  values: list[Any]

  def __init__(self, keys, values):
    self._keys = keys
    self._values = values

  def to_dict(self):
    return dict(zip(self._keys, self._values))

  def tree_flatten(self):
    return self._values, self._keys

  @classmethod
  def tree_unflatten(cls, keys, values):
    return _DictWrapper(keys, values)


class _CantFlatten:
  __repr__ = lambda _: "<cant_flatten>"
cant_flatten = _CantFlatten()

def _safe_flatten_dict(dct: dict[Any, Any]
                       ) -> tuple[list[Any], tree_util.PyTreeDef]:
  # We avoid comparison between keys by just using the original order
  keys, values = [], []
  for key, value in dct.items():
    try:
      tree_util.tree_leaves(value)
    except:
      # If flattening fails, we substitute a sentinel object.
      value = cant_flatten
    keys.append(key)
    values.append(value)
  return tree_util.tree_flatten(_DictWrapper(keys, values))


@tree_util.register_pytree_node_class
@dataclasses.dataclass(frozen=True)
class DebuggerFrame:
  """Encapsulates Python frame information."""
  filename: str
  locals: Dict[str, Any]
  globals: Dict[str, Any]
  code_context: str
  source: List[str]
  lineno: int
  offset: Optional[int]

  def tree_flatten(self):
    flat_locals, locals_tree = _safe_flatten_dict(self.locals)
    flat_globals, globals_tree = _safe_flatten_dict(self.globals)
    flat_vars = flat_locals + flat_globals
    is_valid = [
        isinstance(l, (core.Tracer, jnp.ndarray, np.ndarray))
        for l in flat_vars
    ]
    invalid_vars, valid_vars = util.partition_list(is_valid, flat_vars)
    return valid_vars, (is_valid, invalid_vars, locals_tree, globals_tree,
                        len(flat_locals), self.filename, self.code_context,
                        self.source, self.lineno, self.offset)

  @classmethod
  def tree_unflatten(cls, info, valid_vars):
    (is_valid, invalid_vars, locals_tree, globals_tree, num_locals, filename,
     code_context, source, lineno, offset) = info
    flat_vars = util.merge_lists(is_valid, invalid_vars, valid_vars)
    flat_locals, flat_globals = util.split_list(flat_vars, [num_locals])
    locals_ = tree_util.tree_unflatten(locals_tree, flat_locals).to_dict()
    globals_ = tree_util.tree_unflatten(globals_tree, flat_globals).to_dict()
    return DebuggerFrame(filename, locals_, globals_, code_context, source,
                         lineno, offset)

  @classmethod
  def from_frameinfo(cls, frame_info) -> DebuggerFrame:
    try:
      _, start = inspect.getsourcelines(frame_info.frame)
      source = inspect.getsource(frame_info.frame).split("\n")
      # Line numbers begin at 1 but offsets begin at 0. `inspect.getsource` will
      # return a partial view of the file and a `start` indicating the line
      # number that the source code starts at. However, it's possible that
      # `start` is 0, indicating that we are at the beginning of the file. In
      # this case, `offset` is just the `lineno - 1`. If `start` is nonzero,
      # then we subtract it off from the `lineno` and don't need to subtract 1
      # since both start and lineno are 1-indexed.
      offset = frame_info.lineno - max(start, 1)
    except OSError:
      source = []
      offset = None
    return DebuggerFrame(
        filename=frame_info.filename,
        locals=frame_info.frame.f_locals,
        globals={},
        code_context=frame_info.code_context,
        source=source,
        lineno=frame_info.lineno,
        offset=offset)


class Debugger(Protocol):

  def __call__(self, frames: List[DebuggerFrame], thread_id: Optional[int],
      **kwargs: Any) -> None:
    ...
_debugger_registry: Dict[str, Tuple[int, Debugger]] = {}


def get_debugger(backend: Optional[str] = None) -> Debugger:
  if backend is not None and backend in _debugger_registry:
    return _debugger_registry[backend][1]
  debuggers = sorted(_debugger_registry.values(), key=lambda x: -x[0])
  if not debuggers:
    raise ValueError("No debuggers registered!")
  return debuggers[0][1]


def register_debugger(name: str, debugger: Debugger, priority: int) -> None:
  if name in _debugger_registry:
    raise ValueError(f"Debugger with name \"{name}\" already registered.")
  _debugger_registry[name] = (priority, debugger)


debug_lock = threading.Lock()


def breakpoint(*, backend: Optional[str] = None, filter_frames: bool = True,
               num_frames: Optional[int] = None, ordered: bool = False,
               **kwargs):  # pylint: disable=redefined-builtin
  """Enters a breakpoint at a point in a program.

  Args:
    backend: The debugger backend to use. By default, picks the highest priority
      debugger and in the absence of other registered debuggers, falls back to
      the CLI debugger.
    filter_frames: Whether or not to filter out JAX-internal stack frames from
      the traceback. Since some libraries, like Flax, also make user of JAX's
      stack frame filtering system, this option can also affect whether stack
      frames from libraries are filtered.
    num_frames: The number of frames above the current stack frame to make
      available for inspection in the interactive debugger.
    ordered: A keyword only argument used to indicate whether or not the
      staged out computation will enforce ordering of this ``debug_print``
      with respect to other ordered ``debug_print`` calls.

  Returns:
    None.
  """
  frame_infos = inspect.stack()
  # Throw out first frame corresponding to this function
  frame_infos = frame_infos[1:]
  if num_frames is not None:
    frame_infos = frame_infos[:num_frames]
  # Filter out internal frames
  if filter_frames:
    frames = [
        DebuggerFrame.from_frameinfo(frame_info)
        for frame_info in frame_infos
        if traceback_util.include_frame(frame_info.frame)
    ]
  else:
    frames = [
        DebuggerFrame.from_frameinfo(frame_info)
        for frame_info in frame_infos
    ]
  flat_args, frames_tree = tree_util.tree_flatten(frames)

  def _breakpoint_callback(*flat_args):
    frames = tree_util.tree_unflatten(frames_tree, flat_args)
    thread_id = None
    if threading.current_thread() is not threading.main_thread():
      thread_id = threading.get_ident()
    debugger = get_debugger(backend=backend)
    # Lock here because this could be called from multiple threads at the same
    # time.
    with debug_lock:
      debugger(frames, thread_id, **kwargs)

  debugging.debug_callback(_breakpoint_callback, *flat_args, ordered=ordered)
