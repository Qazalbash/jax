# Copyright 2023 The JAX Authors.
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

"""Test TPU-specific extensions to pallas_call."""

from collections.abc import Callable
import contextlib
import functools
import gc
import io
import itertools
import json
import math
import re
import sys
from absl.testing import absltest
from absl.testing import parameterized
import jax
from jax import api_util
from jax import lax
from jax._src import checkify
from jax._src import shard_map
from jax._src import state
from jax._src import test_util as jtu
from jax._src.interpreters import partial_eval as pe
from jax._src.lib import _jax
from jax._src.state import discharge as state_discharge
from jax._src.state import utils as state_utils
from jax.experimental import mesh_utils
from jax.experimental import mosaic
from jax.experimental import pallas as pl
from jax.experimental.pallas import tpu as pltpu
from jax.experimental.pallas.ops.tpu import example_kernel
from jax.extend import linear_util as lu
import jax.numpy as jnp
import numpy as np


jax.config.parse_flags_with_absl()

P = jax.sharding.PartitionSpec

partial = functools.partial


def only_passes_in_interpret(
    unless_generation: int | None = None, *args, **kwargs
):
  def decorator(f, *args, **kwargs):
    def wrapper(self, *args, **kwargs):
      if self.INTERPRET or (
          unless_generation is not None
          and jtu.is_device_tpu_at_least(unless_generation)
      ):
        f(self, *args, **kwargs)
      else:
        with self.assertRaises(Exception):
          f(self, *args, **kwargs)

    return wrapper

  return decorator


@contextlib.contextmanager
def string_stdout():
  """Redirects stdout to a string."""
  initial_stdout = sys.stdout
  stringio = io.StringIO()
  sys.stdout = stringio
  yield stringio
  sys.stdout = initial_stdout


def wrap_init(f: Callable, nr_args: int):
  # wrapper for lu.wrap_init with debugging info
  return lu.wrap_init(
      f,
      debug_info=api_util.debug_info("state_test", f, (0,) * nr_args, {}))


class PallasBaseTest(jtu.JaxTestCase):
  INTERPRET: bool = False

  def setUp(self):
    if not jtu.test_device_matches(['tpu']) and not self.INTERPRET:
      self.skipTest('Test requires TPUs, or interpret mode')
    super().setUp()

  def pallas_call(self, *args, **kwargs):
    return pl.pallas_call(*args, **kwargs, interpret=self.INTERPRET)

class TPUPipelineModeTest(PallasBaseTest):

  @parameterized.parameters(
      (pl.Buffered(2), pl.Buffered(2)),
      (pl.Buffered(2), pl.Buffered(1)),
      (pl.Buffered(1), pl.Buffered(1)))
  def test_two_input_vadd(self, x_pmode : pl.Buffered, y_pmode : pl.Buffered):
    if not jtu.if_cloud_tpu_at_least(2025, 2, 11):
      self.skipTest("Needs a newer libTPU")
    def body(x_ref, y_ref, o_ref):
      x = x_ref[:]
      y = y_ref[:]
      o_ref[:] = x + y

    size_in_vregs = 128
    data_size = size_in_vregs * 1024
    block_size = 1024

    x = jnp.arange(data_size, dtype=jnp.float32)
    y = jnp.arange(data_size, dtype=jnp.float32)
    in_specs = [
        pl.BlockSpec((block_size,), lambda i: i, pipeline_mode=pmode)
        for pmode in [x_pmode, y_pmode]
    ]
    out_specs = pl.BlockSpec((block_size,), lambda i: i)

    @jax.jit
    def vadd(x, y):
      return self.pallas_call(
        body,
        out_shape=jax.ShapeDtypeStruct(x.shape, jnp.float32),
        in_specs=in_specs,
        out_specs=out_specs,
        grid=data_size // block_size,
    )(x, y)

    compiled = (
        vadd.lower(
            jax.ShapeDtypeStruct(x.shape, x.dtype),
            jax.ShapeDtypeStruct(y.shape, y.dtype),
        )
        .compile()
        .as_text()
    )
    pattern = (
        r'"used_scoped_memory_configs":\[\{"memory_space":"1",.*?"size":"(\d+)"'
    )
    expected_vmem_usage = block_size * 4 * (2 + x_pmode.buffer_count + y_pmode.buffer_count)
    vmem_usage = int(re.search(pattern, compiled).group(1))
    self.assertEqual(vmem_usage, expected_vmem_usage)
    z = vadd(x, y)
    np.testing.assert_allclose(z, x + y)

class PallasCallScalarPrefetchTest(PallasBaseTest):
  def test_trivial_scalar_prefetch(self):
    def body(_, x_ref, o_ref):
      o_ref[...] = x_ref[...]

    s = jnp.array([4, 3, 2, 5, 3, 5, 2, 7], jnp.int32)
    x = jnp.arange(8 * 8 * 128, dtype=jnp.int32).reshape((8 * 8, 128))

    def _x_transform(i, s_ref):
      return (s_ref[i], 0)

    out = self.pallas_call(
        body,
        out_shape=jax.ShapeDtypeStruct(x.shape, jnp.int32),
        grid_spec=pltpu.PrefetchScalarGridSpec(
            num_scalar_prefetch=1,
            in_specs=[
                pl.BlockSpec((x.shape[0] // 8, x.shape[1]), _x_transform),
            ],
            out_specs=pl.BlockSpec(
                (x.shape[0] // 8, x.shape[1]), lambda i, _: (i, 0)
            ),
            grid=8,
        ),
    )(s, x)
    np.testing.assert_allclose(out, x.reshape((8, 8, -1))[s].reshape(x.shape))

  @parameterized.parameters(
      (jnp.bfloat16,),
      (jnp.int8,),
  )
  @only_passes_in_interpret()
  def test_narrow_bitwidth_scalar_prefetch(self, dtype):
    def body(s_ref, x_ref, o_ref):
      o_ref[...] = x_ref[...] + jnp.broadcast_to(s_ref[0], x_ref.shape)

    s = jnp.array([5], dtype)
    x = jnp.arange(16 * 128, dtype=dtype).reshape((16, 128))

    out = self.pallas_call(
        body,
        out_shape=jax.ShapeDtypeStruct(x.shape, dtype),
        grid_spec=pltpu.PrefetchScalarGridSpec(
            num_scalar_prefetch=1,
        ),
    )(s, x)
    jtu.check_close(out, x + jnp.broadcast_to(s, x.shape))

  def test_f32_scalar_prefetch(self):
    def body(s_ref, x_ref, o_ref):
      o_ref[...] = x_ref[...] + jnp.broadcast_to(s_ref[0], x_ref.shape)

    s = jnp.array([5.0], jnp.float32)
    x = jnp.arange(8 * 128, dtype=jnp.float32).reshape((8, 128))

    out = self.pallas_call(
        body,
        out_shape=jax.ShapeDtypeStruct(x.shape, jnp.float32),
        grid_spec=pltpu.PrefetchScalarGridSpec(
            num_scalar_prefetch=1,
        ),
    )(s, x)
    np.testing.assert_allclose(out, x + jnp.broadcast_to(s, x.shape))

  def test_trivial_scalar_prefetch_with_windowless_args(self):
    def body(_, x_ref, o_ref):
      o_ref[...] = x_ref[...]

    s = jnp.array([4, 3, 2, 5, 3, 5, 2, 7], jnp.int32)
    x = jnp.arange(8 * 8 * 128, dtype=jnp.int32).reshape((8 * 8, 128))

    out = self.pallas_call(
        body,
        out_shape=jax.ShapeDtypeStruct(x.shape, jnp.int32),
        grid_spec=pltpu.PrefetchScalarGridSpec(
            num_scalar_prefetch=1,
        ),
    )(s, x)
    np.testing.assert_array_equal(out, x)

  @jtu.parameterized_filterable(
      kwargs=[
          dict(scratch=scratch, vmap=vmap, dyn_grid=dyn_grid)
          for scratch in [True, False]
          for vmap in [False, True]
          for dyn_grid in [False, True]
      ]
  )
  def test_scalar_prefetch_calling_convention(
      self, *,
      scratch: bool, vmap: bool, dyn_grid: bool):
    # Tests what we process correctly all the various inputs and outputs:
    # dynamic_grid_dims, index, inputs, outputs, scratch.
    if jtu.test_device_matches(["cpu"]) and jax.config.x64_enabled:
      self.skipTest("TODO: dslice(start, 1) raises error about slice inputs being int32 and int64")
    to_store = np.arange(128, dtype=np.float32).reshape((1, 128))
    if vmap:
      x_shape = (4, 16, 128)
    else:
      x_shape = (16, 128)
    x = np.arange(math.prod(x_shape), dtype=np.float32).reshape(x_shape)

    def f(x, grid_size, to_store):
      s = jnp.array([1, 0], jnp.int32)  # iteration 0 -> 1, iteration 1 -> 0
      @functools.partial(
          self.pallas_call,
          out_shape=jax.ShapeDtypeStruct((64, 128), x.dtype),
          grid_spec=pltpu.PrefetchScalarGridSpec(
              num_scalar_prefetch=1,  # 1 pytree
              grid=(grid_size,),
              in_specs=[pl.BlockSpec((8, 128),
                                     lambda i, s_ref: (pl.load(s_ref[0], (i,)), 0)),
                        pl.BlockSpec((1, 128), lambda i, s_ref: (0, 0))],
              out_specs=pl.BlockSpec((32, 128),
                                     lambda i, s_ref: (pl.load(s_ref[0], i), 0)),
              scratch_shapes=([pltpu.SemaphoreType.REGULAR((3,))] if scratch
                              else []),
          ),
      )
      def kernel(s_refs, src, to_store, dst, *scratch_refs):
        s_ref, s2, s3 = s_refs
        assert s_ref.shape == (2,)
        assert s2.shape == (3,)
        assert s3 is None
        store_idx = s_ref[pl.program_id(0)]
        dst[pl.dslice(store_idx, 1), :] = to_store[...]
      # Pass a pytree of scalar
      return kernel((s, np.arange(3, dtype=np.int32), None), x, to_store)

    if dyn_grid:
      f = jax.jit(f)
    if vmap:
      res = jax.vmap(lambda x: f(x, 2, to_store))(x)
    else:
      res = f(x, 2, to_store)

    if vmap:
      for i in range(x.shape[0]):
        self.assertAllClose(res[i, 0:1], to_store)
        self.assertAllClose(res[i, 33:34], to_store)
    else:
      self.assertAllClose(res[0:1], to_store)
      self.assertAllClose(res[33:34], to_store)

  def test_with_unhashable_grid_spec(self):
    # Make sure that we don't crash when the GridSpec has non-hashable parts
    @functools.partial(
        self.pallas_call,
        out_shape=[[jax.ShapeDtypeStruct((8, 128), np.int32)]],
        grid_spec=pltpu.PrefetchScalarGridSpec(
            num_scalar_prefetch=1,  # 1 pytree
            grid=(1,),
            in_specs=[[pl.BlockSpec((8, 128),
                                    lambda i, s_ref: (0, 0))]],
            out_specs=[[pl.BlockSpec((8, 128),
                                     lambda i, s_ref: (0, 0))]],
            scratch_shapes=[[pltpu.SemaphoreType.REGULAR((3,))]],
        ),
    )
    def kernel(s_ref, x_ref, o_ref, scratch_ref):
      assert isinstance(s_ref, list)
      assert isinstance(x_ref, list)
      assert isinstance(o_ref, list)
      assert isinstance(scratch_ref, list)
      o_ref[0][...] = x_ref[0][...]

    x_shape = (8, 128)
    s = np.array([0, 1], np.int32)
    x = np.arange(math.prod(x_shape), dtype=np.int32).reshape(x_shape)
    res = kernel([s, s], [x])
    self.assertIsInstance(res, tuple)  # Even though we asked for a list!
    self.assertAllClose(res[0][0], x)

  def test_vmap_scalar_prefetch(self):
    def body(_, x_ref, o_ref):
      o_ref[...] = x_ref[...]

    s = jnp.array([4, 3, 2, 5, 3, 5, 2, 7], jnp.int32)
    x = jnp.arange(2 * 8 * 8 * 128, dtype=jnp.int32).reshape((2, 8 * 8, 128))

    def _x_transform(i, s_ref):
      s = s_ref[i]
      return (s, 0)

    def f(x):
      return self.pallas_call(
          body,
          out_shape=jax.ShapeDtypeStruct(x.shape, jnp.int32),
          grid_spec=pltpu.PrefetchScalarGridSpec(
              num_scalar_prefetch=1,
              in_specs=[
                  pl.BlockSpec((x.shape[0] // 8, x.shape[1]), _x_transform),
              ],
              out_specs=pl.BlockSpec(
                  (x.shape[0] // 8, x.shape[1]), lambda i, _: (i, 0)
              ),
              grid=8),
      )(s, x)
    np.testing.assert_allclose(
        jax.vmap(f)(x), x.reshape((2, 8, 8, -1))[:, s].reshape(x.shape)
    )

  def test_multiple_scalar_prefetch(self):
    def body(s1_ref, s2_ref, x_ref, o_ref):
      del s1_ref, s2_ref
      o_ref[...] = x_ref[...]

    s1 = jnp.array([4, 3, 2, 5, 3, 5, 2, 7], jnp.int32)
    s2 = jnp.array([7, 6, 5, 4, 3, 2, 1, 0], jnp.int32)
    x = jnp.arange(64 * 128, dtype=jnp.int32).reshape((64, 128))

    def _x_transform(i, s1_ref, _):
      return s1_ref[i], 0

    def _o_transform(i, _, s2_ref):
      return s2_ref[i], 0

    out = self.pallas_call(
        body,
        out_shape=jax.ShapeDtypeStruct((64, 128), jnp.int32),
        grid_spec=pltpu.PrefetchScalarGridSpec(
            num_scalar_prefetch=2,
            in_specs=[
                pl.BlockSpec((8, 128), _x_transform),
            ],
            out_specs=pl.BlockSpec((8, 128), _o_transform),
            grid=8,
        ),
    )(s1, s2, x)
    out_ref = x.reshape((8, 8, -1))[s1][::-1].reshape((64, 128))
    np.testing.assert_allclose(out, out_ref)

  def test_scalar_interpreter(self):
    program = jnp.array([0, 0, 1, 0, 1, 1], jnp.int32)
    x = jnp.arange(8 * 8 * 128.0, dtype=jnp.float32).reshape(8 * 8, 128)

    def body(sprogram_ref, x_ref, o_ref, state_ref):
      x = x_ref[...]

      def add_branch_fn(j):
        state_ref[...] += jnp.float32(j)
        return ()

      def mult_branch_fn(j):
        state_ref[...] *= jnp.float32(j)
        return ()

      def single_inst(i, _):
        _ = jax.lax.switch(
            sprogram_ref[i],
            (
                add_branch_fn,
                mult_branch_fn,
            ),
            i,
        )

      # We can't use for loop state right now, because Pallas functionalizes it,
      # and Mosaic support for returning values form scf.if is incomplete.
      state_ref[...] = x
      lax.fori_loop(0, sprogram_ref.shape[0], single_inst, None, unroll=True)
      o_ref[...] = state_ref[...]

    # Ignore the scratch output.
    out, _ = self.pallas_call(
        body,
        out_shape=[
            jax.ShapeDtypeStruct(x.shape, jnp.float32),
            jax.ShapeDtypeStruct((8, 128), jnp.float32),
        ],
        grid_spec=pltpu.PrefetchScalarGridSpec(
            num_scalar_prefetch=1,
            in_specs=[pl.BlockSpec((8, 128), lambda i, *_: (i, 0))],
            out_specs=[
                pl.BlockSpec((8, 128), lambda i, *_: (i, 0)),
                pl.BlockSpec((8, 128), lambda *_: (0, 0)),
            ],
            grid=8,
        ),
    )(program, x)

    expected = x
    for i, p in enumerate(program):
      if p == 0:
        expected += i
      elif p == 1:
        expected *= i

    np.testing.assert_allclose(out, expected)

  def test_scalar_interpreter_dynamic_loop(self):
    loop_end = jnp.array([5], jnp.int32)

    def body(loop_end_ref, out_ref):
      out_ref[...] = jnp.zeros_like(out_ref)

      def loop_body(i, carry):
        del i, carry
        out_ref[...] += 1

      lax.fori_loop(0, loop_end_ref[0], loop_body, None)

    out = self.pallas_call(
        body,
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
        grid_spec=pltpu.PrefetchScalarGridSpec(
            num_scalar_prefetch=1,
            out_specs=pl.BlockSpec((8, 128), lambda *_: (0, 0)),
            grid=1,
        ),
    )(loop_end)

    expected_out = jnp.ones((8, 128), jnp.float32) * 5
    np.testing.assert_allclose(out, expected_out)

  def test_vmap_scalar_prefetch_1sized(self):
    def body(_, x_ref, o_ref):
      o_ref[...] = x_ref[...]

    s = jnp.array([4, 3, 2, 5, 3, 5, 2, 7], jnp.int32)
    x = jnp.arange(8 * 8 * 128, dtype=jnp.int32).reshape((8 * 8, 128))

    def _x_transform(i, s_ref):
      s = s_ref[i]
      return (s, 0)

    s = s[None]
    x = x[None]

    out = jax.vmap(
        self.pallas_call(
            body,
            out_shape=jax.ShapeDtypeStruct(x.shape[1:], x.dtype),
            grid_spec=pltpu.PrefetchScalarGridSpec(
                num_scalar_prefetch=1,
                in_specs=[
                    pl.BlockSpec((x.shape[1] // 8, x.shape[2]), _x_transform),
                ],
                out_specs=pl.BlockSpec(
                    (x.shape[1] // 8, x.shape[2]), lambda i, _: (i, 0)
                ),
                grid=8,
            ),
        )
    )(s, x)
    np.testing.assert_allclose(
        out, x.reshape((1, 8, 8, -1))[:, s].reshape(x.shape)
    )

  def test_nontrivial_vmap_scalar_prefetch(self):
    def body(_, x_ref, o_ref):
      o_ref[...] = x_ref[...]

    s = jnp.array([4, 3, 2, 5, 3, 5, 2, 7], jnp.int32)
    x = jnp.arange(2 * 8 * 8 * 128, dtype=jnp.int32).reshape((2, 8 * 8, 128))

    def _x_transform(i, s_ref):
      s = s_ref[i]
      return (s, 0)

    s = jnp.tile(s[None], [2, 1])

    @jax.jit
    @jax.vmap
    def kernel(s, x):
      return self.pallas_call(
          body,
          out_shape=jax.ShapeDtypeStruct(x.shape, x.dtype),
          grid_spec=pltpu.PrefetchScalarGridSpec(
              num_scalar_prefetch=1,
              in_specs=[
                  pl.BlockSpec((x.shape[0] // 8, x.shape[1]), _x_transform),
              ],
              out_specs=pl.BlockSpec(
                  (x.shape[0] // 8, x.shape[1]), lambda i, _: (i, 0)
              ),
              grid=8,
          ),
          compiler_params=pltpu.CompilerParams(
              allow_input_fusion=[False, True]
          ),
      )(s, x)

    first = x[0, ...].reshape((1, 8, 8, -1))[:, s[0, ...]].reshape(x.shape[1:])
    second = x[1, ...].reshape((1, 8, 8, -1))[:, s[1, ...]].reshape(x.shape[1:])

    expected = jnp.stack([first, second])
    np.testing.assert_allclose(kernel(s, x), expected)

  def test_input_output_aliasing_with_scalar_prefetch(self):
    x = jnp.ones((32, 1024, 1024))
    expected = x + 1

    def kernel(_, x_ref, y_ref):
      y_ref[...] = x_ref[...] + 1.
    @partial(jax.jit, donate_argnums=(0,))
    def f(x):
      return self.pallas_call(
          kernel,
          out_shape=x,
          grid_spec=pltpu.PrefetchScalarGridSpec(
              num_scalar_prefetch=1,
              in_specs=[
                  pl.BlockSpec((None, 1024, 1024), lambda i, _: (i, 0, 0))
              ],
              out_specs=pl.BlockSpec(
                  (None, 1024, 1024), lambda i, _: (i, 0, 0)
              ),
              grid=(x.shape[0],),
          ),
          input_output_aliases={1: 0},
      )(jnp.array([1, 2, 3]), x)
    o = f(x)
    np.testing.assert_array_equal(o, expected)
    compiled = f.lower(jax.ShapeDtypeStruct(x.shape, x.dtype)).compile()
    mem_analysis = compiled.memory_analysis()
    expected_num_bytes = np.prod(x.shape) * x.dtype.itemsize
    self.assertEqual(mem_analysis.alias_size_in_bytes, expected_num_bytes)


class PallasCallScalarPrefetchInterpretTest(PallasCallScalarPrefetchTest):
  INTERPRET: bool = True


class PallasCallDynamicGridTest(PallasBaseTest):

  def test_can_query_grid_statically_via_num_programs(self):

    def kernel(_):
      num_programs = pl.num_programs(0)
      self.assertIsInstance(num_programs, int)
      self.assertEqual(num_programs, 2)

    self.pallas_call(kernel, out_shape=None, grid=(2,))()

  def test_can_query_grid_statically_via_num_programs_in_block_spec(self):

    def kernel(*_):
      pass

    def x_index_map(_):
      num_programs = pl.num_programs(0)
      self.assertIsInstance(num_programs, int)
      self.assertEqual(num_programs, 2)
      return 0, 0
    self.pallas_call(
        kernel,
        in_specs=[pl.BlockSpec((8, 128), x_index_map)],
        out_shape=None,
        grid=(2,),
    )(jnp.ones((8, 128)))

  def test_dynamic_grid_has_dynamic_size(self):

    def kernel(_):
      num_programs = pl.num_programs(0)
      self.assertIsInstance(num_programs, int, msg=type(num_programs))
      self.assertEqual(num_programs, 2)
      num_programs = pl.num_programs(1)
      self.assertIsInstance(num_programs, jax.Array)

    @jax.jit
    def outer(x):
      self.pallas_call(kernel, out_shape=None, grid=(2, x))()
    outer(2)

  def test_dynamic_grid(self):
    shape = (8, 128)
    result_ty = jax.ShapeDtypeStruct(shape, jnp.float32)

    def kernel(y_ref):
      @pl.when(pl.program_id(0) == 0)
      def _init():
        y_ref[...] = jnp.zeros_like(y_ref)
      y_ref[...] += 1

    @jax.jit
    def dynamic_kernel(steps):
      return self.pallas_call(
          kernel,
          grid=(steps * 2,),
          out_specs=pl.BlockSpec(shape, lambda i: (0, 0)),
          out_shape=result_ty,
      )()
    np.testing.assert_array_equal(
        dynamic_kernel(jnp.int32(4)), np.full(shape, 8.0, np.float32)
    )

  def test_dynamic_grid_overflow(self):
    # If we pad statically the dynamic grid dims to max int32, then the product
    # of this grid size will overflow int64 and can cause failing checks in XLA.
    shape = (8, 128)
    result_ty = jax.ShapeDtypeStruct(shape, jnp.float32)

    def kernel(y_ref):
      @pl.when(sum(pl.program_id(i) for i in range(3)) == 0)
      def _init():
        y_ref[...] = jnp.zeros_like(y_ref)
      y_ref[...] += 1

    @jax.jit
    def dynamic_kernel(steps):
      return self.pallas_call(
          kernel,
          grid=(steps * 2, steps + 1, 3),
          out_specs=pl.BlockSpec(shape, lambda *_: (0, 0)),
          out_shape=result_ty,
      )()
    np.testing.assert_array_equal(
        dynamic_kernel(jnp.int32(4)), np.full(shape, 120.0, np.float32)
    )

  # TODO(apaszke): Add tests for scalar_prefetch too
  def test_dynamic_grid_scalar_input(self):
    shape = (8, 128)
    result_ty = jax.ShapeDtypeStruct(shape, jnp.float32)

    def kernel(scalar_input_ref, output_ref):
      output_ref[...] = jnp.full_like(output_ref, scalar_input_ref[0, 0])

    @jax.jit
    def dynamic_kernel(steps):
      return self.pallas_call(
          kernel,
          out_shape=result_ty,
          in_specs=[pl.BlockSpec(memory_space=pltpu.SMEM)],
          out_specs=pl.BlockSpec(shape, lambda i: (0, 0)),
          grid=(steps * 2,),
      )(jnp.array([[42]], dtype=jnp.int32))

    np.testing.assert_array_equal(
        dynamic_kernel(jnp.int32(4)), np.full(shape, 42.0, np.float32)
    )

  def test_vmap_trivial_dynamic_grid(self):
    shape = (8, 128)
    result_ty = jax.ShapeDtypeStruct(shape, jnp.float32)

    def kernel(x_ref, y_ref):
      @pl.when(pl.program_id(0) == 0)
      def _init():
        y_ref[...] = x_ref[...]
      y_ref[...] += 1

    @jax.jit
    @jax.vmap
    def dynamic_kernel(steps, x):
      return self.pallas_call(
          kernel,
          grid=(steps * 2,),
          in_specs=[pl.BlockSpec(shape, lambda i: (0, 0))],
          out_specs=pl.BlockSpec(shape, lambda i: (0, 0)),
          out_shape=result_ty,
      )(x)
    x = jnp.arange(8 * 128., dtype=jnp.float32).reshape((1, *shape))
    np.testing.assert_array_equal(
        dynamic_kernel(jnp.array([4], jnp.int32), x), x + 8.0
    )

  def test_vmap_nontrivial_dynamic_grid(self):
    # Dynamic grid doesn't support vmapping over multiple distinct grid values
    # at the moment.
    shape = (8, 128)
    result_ty = jax.ShapeDtypeStruct(shape, jnp.float32)

    def kernel(y_ref):
      @pl.when(pl.program_id(0) == 0)
      def _init():
        y_ref[...] = jnp.zeros_like(y_ref)
      y_ref[...] += 1

    @jax.jit
    @jax.vmap
    def dynamic_kernel(steps):
      return self.pallas_call(
          kernel,
          grid=(steps * 2,),
          out_specs=pl.BlockSpec(shape, lambda i: (0, 0)),
          out_shape=result_ty,
      )()
    out = dynamic_kernel(jnp.array([4, 8], jnp.int32))
    first = jnp.full(shape, fill_value=8.0, dtype=jnp.float32)
    second = jnp.full(shape, fill_value=16.0, dtype=jnp.float32)
    expected_out = jnp.stack([first, second], axis=0)
    np.testing.assert_array_equal(out, expected_out)

  def test_vmap_dynamic_grid(self):
    shape = (8, 128)
    result_ty = jax.ShapeDtypeStruct(shape, jnp.float32)

    def kernel(x_ref, y_ref):
      @pl.when(pl.program_id(0) == 0)
      def _init():
        y_ref[...] = x_ref[...]
      y_ref[...] += jnp.float32(1.)

    @jax.jit
    def dynamic_kernel(x, steps):
      return self.pallas_call(
          kernel,
          grid=(steps * 2,),
          out_specs=pl.BlockSpec(shape, lambda i: (0, 0)),
          out_shape=result_ty,
      )(x)
    x = jnp.arange(4 * 8 * 128., dtype=jnp.float32).reshape((4, *shape))
    np.testing.assert_array_equal(
        jax.jit(jax.vmap(dynamic_kernel, in_axes=(0, None)))(x, jnp.int32(4)),
        x + 8,
    )

  def test_num_programs(self):
    def kernel(y_ref):
      y_ref[0, 0] = pl.num_programs(0)

    @jax.jit
    def dynamic_kernel(steps):
      return self.pallas_call(
          kernel,
          grid=(steps * 2,),
          out_specs=pl.BlockSpec(memory_space=pltpu.SMEM),
          out_shape=jax.ShapeDtypeStruct((1, 1), jnp.int32),
      )()

    self.assertEqual(dynamic_kernel(np.int32(4)), 8)

  @parameterized.parameters(range(1, 4))
  def test_vmap_num_programs(self, num_vmaps):
    result_ty = jax.ShapeDtypeStruct((8, 128), jnp.int32)

    def kernel(y_ref):
      y_ref[...] = jnp.full_like(y_ref, pl.num_programs(0))

    kernel_call = self.pallas_call(
        kernel,
        grid=(8,),
        out_specs=pl.BlockSpec(result_ty.shape, lambda i: (0, 0)),
        out_shape=result_ty,
    )

    out_shape = (*(2 for _ in range(num_vmaps)), *result_ty.shape)
    f = kernel_call
    for _ in range(num_vmaps):
      f = lambda impl=f: jax.vmap(impl, axis_size=2)()
    out = jax.jit(f)()
    np.testing.assert_array_equal(out, np.full(out_shape, 8.0))

  def test_num_programs_block_spec(self):
    def kernel(x_ref, y_ref):
      y_ref[...] = x_ref[...]

    @jax.jit
    def dynamic_kernel(steps, x):
      return self.pallas_call(
          kernel,
          grid=(steps * 2,),
          in_specs=[
              pl.BlockSpec(
                  (8, 128),
                  # Should always evaluate to (1, 0)
                  lambda i: (1 + 8 - pl.num_programs(0), 0),
              )
          ],
          out_specs=pl.BlockSpec((8, 128), lambda i: (0, 0)),
          out_shape=jax.ShapeDtypeStruct((8, 128), jnp.int32),
      )(x)

    x = np.arange(4 * 8 * 128., dtype=np.int32).reshape((4 * 8, 128))
    np.testing.assert_array_equal(dynamic_kernel(np.int32(4), x), x[8:16])


class PallasCallDynamicGridInterpretTest(PallasCallDynamicGridTest):
  INTERPRET = True


class PallasCallDMATest(PallasBaseTest):

  def setUp(self):
    super().setUp()
    if not jtu.is_device_tpu_at_least(4):
      self.skipTest('DMAs not supported on TPU generations <= 3')

  def test_can_have_unspecified_memory_spaces(self):
    def kernel(x_ref, y_ref):
      # Just test whether things compile
      del x_ref, y_ref

    x = jnp.ones((8, 128), dtype=jnp.float32)
    y = self.pallas_call(
        kernel,
        in_specs=[pl.BlockSpec(memory_space=pl.ANY)],
        out_specs=pl.BlockSpec(memory_space=pl.ANY),
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )(x)
    jax.block_until_ready(y)

  def test_run_scoped_tracks_effects(self):
    def kernel(x_ref, y_ref):
      def body(temp_ref):
        temp_ref[...] = jnp.ones_like(temp_ref)
        x_ref[...] = 4 * y_ref[...] + temp_ref[...]

      pl.run_scoped(body, pltpu.VMEM((8,), jnp.float32))
      return []

    jaxpr, _, _ = pe.trace_to_jaxpr_dynamic(
        wrap_init(kernel, 2),
        [
            state.shaped_array_ref((8,), jnp.float32),
            state.shaped_array_ref((8,), jnp.float32),
        ],
    )
    expected_effects = {state.ReadEffect(1), state.WriteEffect(0)}
    self.assertSetEqual(jaxpr.effects, expected_effects)

  def test_scoped_allocation(self):
    def kernel(y_ref):
      def body(x_ref):
        x_ref[...] = jnp.ones_like(x_ref)
        y_ref[...] = 4 * x_ref[...]

      pl.run_scoped(body, pltpu.VMEM((8, 128), jnp.float32))

    o = self.pallas_call(
        kernel,
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )()
    np.testing.assert_allclose(o, 4 * np.ones_like(o))

  def test_run_scoped_can_return_scalar_value(self):
    def kernel(y_ref):
      def body(x_ref):
        x_ref[0] = 0
        x_ref[0] += 1
        return x_ref[0] + 2

      out = pl.run_scoped(body, pltpu.SMEM((1,), jnp.int32))
      y_ref[0] = out

    o = self.pallas_call(
        kernel,
        grid_spec=pltpu.PrefetchScalarGridSpec(
            num_scalar_prefetch=0,
            out_specs=pl.BlockSpec(memory_space=pltpu.SMEM),
        ),
        out_shape=jax.ShapeDtypeStruct((1,), jnp.int32),
    )()
    np.testing.assert_allclose(o, jnp.array([3], jnp.int32))

  def test_run_scoped_can_return_scalar_values(self):
    def kernel(y_ref):
      def body(x_ref):
        x_ref[0] = 0
        x_ref[0] += 1
        return x_ref[0] + 2, x_ref[0]

      out = pl.run_scoped(body, pltpu.SMEM((1,), jnp.int32))
      y_ref[0], y_ref[1] = out

    o = self.pallas_call(
        kernel,
        grid_spec=pltpu.PrefetchScalarGridSpec(
            num_scalar_prefetch=0,
            out_specs=pl.BlockSpec(memory_space=pltpu.SMEM),
        ),
        out_shape=jax.ShapeDtypeStruct((2,), jnp.int32),
    )()
    np.testing.assert_allclose(o, jnp.array([3, 1], jnp.int32))

  def test_run_scoped_can_return_vector_values(self):
    def kernel(y_ref):
      def body(x_ref):
        x_ref[...] = jnp.ones_like(x_ref)
        return x_ref[...] + 1

      out = pl.run_scoped(body, pltpu.VMEM((16, 128), jnp.int32))
      y_ref[...] = out

    o = self.pallas_call(
        kernel,
        grid_spec=pltpu.PrefetchScalarGridSpec(
            num_scalar_prefetch=0,
            out_specs=pl.BlockSpec(memory_space=pltpu.VMEM),
        ),
        out_shape=jax.ShapeDtypeStruct((16, 128), jnp.int32),
    )()
    np.testing.assert_allclose(o, jnp.full((16, 128), 2, dtype=jnp.int32))

  def test_run_scoped_can_return_padded_vector_values(self):
    def kernel(y_ref):
      def body(x_ref):
        x_ref[...] = jnp.ones_like(x_ref)
        return x_ref[...] + 1

      out = pl.run_scoped(body, pltpu.VMEM((17, 128), jnp.int32))
      y_ref[...] = out

    o = self.pallas_call(
        kernel,
        grid_spec=pltpu.PrefetchScalarGridSpec(
            num_scalar_prefetch=0,
            out_specs=pl.BlockSpec(memory_space=pltpu.VMEM),
        ),
        out_shape=jax.ShapeDtypeStruct((17, 128), jnp.int32),
    )()
    np.testing.assert_allclose(o, jnp.full((17, 128), 2, dtype=jnp.int32))

  def test_nested_scoped_allocation(self):
    def kernel(y_ref):
      def body(x_ref):
        x_ref[...] = jnp.zeros_like(x_ref)
        def inner_body(z_ref):
          z_ref[...] = jnp.ones_like(z_ref)
          x_ref[...] = z_ref[...]
        pl.run_scoped(inner_body, pltpu.VMEM((8, 128), jnp.float32))
        y_ref[...] = 4 * x_ref[...]
      pl.run_scoped(body, pltpu.VMEM((8, 128), jnp.float32))

    o = self.pallas_call(
        kernel,
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )()
    np.testing.assert_allclose(o, 4 * np.ones_like(o))

  def test_run_scoped_partial_discharge(self):
    def f(a_ref, b_ref):
      def scope():
        a_ref[...] = jnp.ones(4, jnp.float32)
        b_ref[...] = jnp.ones(4, jnp.float32)
        return []
      pl.run_scoped(scope)
      return []

    aref1 = state.AbstractRef(jax.core.ShapedArray((4,), jnp.dtype('float32')))
    aref2 = state.AbstractRef(jax.core.ShapedArray((4,), jnp.dtype('float32')))
    in_avals = [aref1, aref2]
    stateful_jaxpr, _, () = pe.trace_to_jaxpr_dynamic(wrap_init(f, 2), in_avals)
    discharged_jaxpr, _ = state_discharge.discharge_state(
        stateful_jaxpr, consts=(), should_discharge=[False, True])
    self.assertLen(discharged_jaxpr.invars, 2)
    self.assertLen(discharged_jaxpr.outvars, 1)
    self.assertIsInstance(discharged_jaxpr.invars[0].aval, state.AbstractRef)
    self.assertIsInstance(discharged_jaxpr.invars[1].aval, jax.core.ShapedArray)
    self.assertEqual(discharged_jaxpr.effects, {state.WriteEffect(0)})

  def test_can_allocate_semaphore(self):
    def kernel(y_ref):
      def body(sem1):
        pass
      pl.run_scoped(body, pltpu.SemaphoreType.DMA)

    jax.block_until_ready(self.pallas_call(
        kernel,
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )())

  def test_can_allocate_multiple_semaphores(self):
    def kernel(y_ref):
      def body(sem1, sem2):
        pass
      pl.run_scoped(body, pltpu.SemaphoreType.DMA, pltpu.SemaphoreType.REGULAR)

    jax.block_until_ready(self.pallas_call(
        kernel,
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )())

  def test_can_allocate_semaphore_array(self):
    def kernel(y_ref):
      def body(dma_sems, sems):
        self.assertTupleEqual(dma_sems.shape, (4,))
        self.assertTupleEqual(sems.shape, (3,))
        self.assertTrue(jnp.issubdtype(dma_sems.dtype, pltpu.dma_semaphore))
        self.assertTrue(jnp.issubdtype(sems.dtype, pltpu.semaphore))
      pl.run_scoped(
          body, pltpu.SemaphoreType.DMA((4,)), pltpu.SemaphoreType.REGULAR((3,))
      )

    jax.block_until_ready(self.pallas_call(
        kernel,
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )())

  def test_can_allocate_scratch_semaphore_array(self):
    def kernel(y_ref, dma_sems, sems):
      self.assertTupleEqual(dma_sems.shape, (4,))
      self.assertTupleEqual(sems.shape, (3,))
      self.assertTrue(jnp.issubdtype(dma_sems.dtype, pltpu.dma_semaphore))
      self.assertTrue(jnp.issubdtype(sems.dtype, pltpu.semaphore))

    jax.block_until_ready(
        self.pallas_call(
            kernel,
            out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
            grid_spec=pltpu.PrefetchScalarGridSpec(
                num_scalar_prefetch=0,
                scratch_shapes=[
                    pltpu.SemaphoreType.DMA((4,)),
                    pltpu.SemaphoreType.REGULAR((3,)),
                ],
            ),
        )()
    )

  def test_can_wait_on_semaphore(self):
    def kernel(y_ref):
      def body(sem):
        pltpu.semaphore_signal(sem)
        pltpu.semaphore_wait(sem)
      pl.run_scoped(body, pltpu.SemaphoreType.REGULAR)
      def body2(sem):
        pltpu.semaphore_signal(sem, 2)
        pltpu.semaphore_wait(sem)
        pltpu.semaphore_wait(sem)
      pl.run_scoped(body2, pltpu.SemaphoreType.REGULAR)
      def body3(sem):
        pltpu.semaphore_signal(sem)
        pltpu.semaphore_signal(sem)
        pltpu.semaphore_signal(sem)
        pltpu.semaphore_wait(sem)
        pltpu.semaphore_wait(sem)
        pltpu.semaphore_wait(sem)
      pl.run_scoped(body3, pltpu.SemaphoreType.REGULAR)

    # TODO(b/345534352): Add interpret support for semaphore signal/wait.
    jax.block_until_ready(self.pallas_call(
        kernel,
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )())

  def test_can_wait_on_semaphore_array(self):
    def kernel(y_ref):
      def body(sems):
        pltpu.semaphore_signal(sems.at[0])
        pltpu.semaphore_wait(sems.at[0])

        pltpu.semaphore_signal(sems.at[1], 2)
        pltpu.semaphore_wait(sems.at[1])
        pltpu.semaphore_wait(sems.at[1])

        pltpu.semaphore_signal(sems.at[2])
        pltpu.semaphore_signal(sems.at[2])
        pltpu.semaphore_signal(sems.at[2])
        pltpu.semaphore_wait(sems.at[2])
        pltpu.semaphore_wait(sems.at[2])
        pltpu.semaphore_wait(sems.at[2])
      pl.run_scoped(body, pltpu.SemaphoreType.REGULAR((3,)))

    # TODO(b/345534352): Add interpret support for semaphore signal/wait.
    jax.block_until_ready(self.pallas_call(
        kernel,
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )())

  def test_can_wait_on_semaphore_array_with_dynamic_index(self):
    def kernel(y_ref):
      i = pl.program_id(0)
      def body(sems):
        pltpu.semaphore_signal(sems.at[i, 0])
        pltpu.semaphore_wait(sems.at[i, 0])

        pltpu.semaphore_signal(sems.at[i, 1], 2)
        pltpu.semaphore_wait(sems.at[i, 1])
        pltpu.semaphore_wait(sems.at[i, 1])

        pltpu.semaphore_signal(sems.at[i, 2])
        pltpu.semaphore_signal(sems.at[i, 2])
        pltpu.semaphore_signal(sems.at[i, 2])
        pltpu.semaphore_wait(sems.at[i, 2])
        pltpu.semaphore_wait(sems.at[i, 2])
        pltpu.semaphore_wait(sems.at[i, 2])
      pl.run_scoped(body, pltpu.SemaphoreType.REGULAR((4, 3)))

    jax.block_until_ready(
        self.pallas_call(
            kernel,
            in_specs=[],
            out_specs=pl.BlockSpec((8, 128), lambda i: (0, 0)),
            out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
            grid=4,
        )()
    )

  def test_can_read_semaphore(self):
    m, n = 2, 3

    def kernel(y_ref):
      def body(sems):
        for r in range(m):
          for c in range(n):
            v = r * n + c
            pltpu.semaphore_signal(sems.at[r, c],v)
            y_ref[r, c] = pltpu.semaphore_read(sems.at[r, c])
            pltpu.semaphore_wait(sems.at[r, c], v)

      pl.run_scoped(body, pltpu.SemaphoreType.REGULAR((m, n)))

    y = jax.block_until_ready(
        self.pallas_call(
            kernel,
            out_specs=pl.BlockSpec(memory_space=pltpu.SMEM),
            out_shape=jax.ShapeDtypeStruct((m, n), jnp.int32),
        )()
    )
    np.testing.assert_array_equal(
        y, jnp.arange(m * n).astype(jnp.int32).reshape((m, n))
    )

  def test_can_read_dma_semaphore(self):
    def kernel(x_hbm_ref, y_hbm_ref, sem_val_ref, dma_sem):
      sem_val_ref[0, 0] = 123
      pltpu.async_copy(x_hbm_ref, y_hbm_ref, dma_sem).wait()
      sem_val_ref[0, 0] = pltpu.semaphore_read(dma_sem)

    x = jnp.arange(8 * 128, dtype=jnp.int32).reshape((8, 128))
    y, sem_val = jax.block_until_ready(
        self.pallas_call(
            kernel,
            grid_spec=pltpu.PrefetchScalarGridSpec(
                num_scalar_prefetch=0,
                in_specs=[pl.BlockSpec(memory_space=pl.ANY)],
                out_specs=[
                    pl.BlockSpec(memory_space=pl.ANY),
                    pl.BlockSpec(memory_space=pltpu.SMEM),
                ],
                scratch_shapes=[pltpu.SemaphoreType.DMA],
            ),
            out_shape=[
                jax.ShapeDtypeStruct((8, 128), jnp.int32),
                jax.ShapeDtypeStruct((1, 1), jnp.int32),
            ],
        )(x)
    )
    np.testing.assert_array_equal(y, x)
    np.testing.assert_array_equal(sem_val, 0)

  def test_set_dma_priority(self):
    if not jtu.if_cloud_tpu_at_least(2025, 4, 5):
      self.skipTest('Needs a newer libTPU')
    if jtu.get_tpu_version() < 5:
      self.skipTest('Target does not support DMA prefetch between HBM and VMEM')
    def kernel(x1, x2, y1, y2, scratch1, scratch2, sem1, sem2):
      copy1 = pltpu.async_copy(x1, scratch1, sem1, priority=1)
      copy2 = pltpu.async_copy(x2, scratch2, sem2, priority=0)
      copy1.wait()
      copy2.wait()
      copy1 = pltpu.async_copy(scratch1, y1, sem1, priority=0)
      copy2 = pltpu.async_copy(scratch2, y2, sem2, priority=1)
      copy1.wait()
      copy2.wait()

    shape = (8, 128)
    dtype = jnp.int32
    x1 = jnp.arange(np.prod(shape), dtype=dtype).reshape(shape)
    x2 = x1 + 1
    y1, y2 = self.pallas_call(
        kernel,
        grid_spec=pltpu.PrefetchScalarGridSpec(
            num_scalar_prefetch=0,
            in_specs=[pl.BlockSpec(memory_space=pl.ANY)] * 2,
            scratch_shapes=[pltpu.VMEM(shape, dtype)] * 2
            + [pltpu.SemaphoreType.DMA] * 2,
            out_specs=[pl.BlockSpec(memory_space=pl.ANY)] * 2,
        ),
        out_shape=[jax.ShapeDtypeStruct(shape, dtype)] * 2,
    )(x1, x2)
    np.testing.assert_array_equal(y1, x1)
    np.testing.assert_array_equal(y2, x2)

  def test_hbm_hbm_dma(self):
    def kernel(x_hbm_ref, y_hbm_ref):
      def body(sem):
        pltpu.async_copy(x_hbm_ref.at[:8, :], y_hbm_ref.at[:, :128], sem).wait()
      pl.run_scoped(body, pltpu.SemaphoreType.DMA)
    x = jnp.arange(8 * 128.).reshape((8, 128))
    y = self.pallas_call(
        kernel,
        in_specs=[
            pl.BlockSpec(memory_space=pl.ANY),
        ],
        out_specs=pl.BlockSpec(memory_space=pl.ANY),
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )(x)
    np.testing.assert_array_equal(y, x)

  def test_host_input_host_to_hbm_dma(self):
    if self.INTERPRET:
      self.skipTest('Interpret mode does not support host memory.')
    if not jtu.if_cloud_tpu_at_least(2025, 7, 12):
      self.skipTest("Requires libtpu built after 2025-07-12")
    def kernel(x_host_ref, y_hbm_ref):
      def body(sem):
        pltpu.async_copy(x_host_ref, y_hbm_ref, sem).wait()

      pl.run_scoped(body, pltpu.SemaphoreType.DMA)

    x = jnp.arange(8 * 128.0).reshape((8, 128))
    # Move input to the host.
    x = jax.device_put(
        x,
        jax.sharding.NamedSharding(
            jax.sharding.Mesh(jax.devices(), 'x'),
            jax.sharding.PartitionSpec(),
            memory_kind='pinned_host',
        ),
    )
    y = self.pallas_call(
        kernel,
        in_specs=[
            pl.BlockSpec(memory_space=pl.HOST),
        ],
        out_specs=pl.BlockSpec(memory_space=pl.ANY),
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )(x)
    np.testing.assert_array_equal(y, x)

  def test_host_input_hbm_to_host_dma(self):
    if self.INTERPRET:
      self.skipTest('Interpret mode does not support host memory.')
    if not jtu.if_cloud_tpu_at_least(2025, 7, 12):
      self.skipTest("Requires libtpu built after 2025-07-12")
    def kernel(x_host_ref, y_hbm_ref, _):
      def body(sem):
        pltpu.async_copy(y_hbm_ref, x_host_ref, sem).wait()

      pl.run_scoped(body, pltpu.SemaphoreType.DMA)

    x = jnp.arange(8 * 128.0).reshape((8, 128))
    y = jnp.ones((8, 128))
    # Move input to the host.
    x = jax.device_put(
        x,
        jax.sharding.NamedSharding(
            jax.sharding.Mesh(jax.devices(), 'x'),
            jax.sharding.PartitionSpec(),
            memory_kind='pinned_host',
        ),
    )
    z = self.pallas_call(
        kernel,
        in_specs=[
            pl.BlockSpec(memory_space=pl.HOST),
            pl.BlockSpec(memory_space=pl.ANY),
        ],
        out_specs=pl.BlockSpec(memory_space=pl.ANY),
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )(x, y)
    np.testing.assert_array_equal(x, y)

  def test_cannot_dma_with_nonscalar_semaphore_ref(self):
    def kernel(x_hbm_ref, y_hbm_ref):
      def body(sem):
        pltpu.async_copy(x_hbm_ref.at[pl.ds(8), :], y_hbm_ref.at[:, pl.ds(128)],
                         sem).wait()
      pl.run_scoped(body, pltpu.SemaphoreType.DMA((1,)))

    with self.assertRaisesRegex(ValueError, 'Cannot signal'):
      x = jnp.arange(8 * 128.).reshape((8, 128))
      self.pallas_call(
          kernel,
          in_specs=[
              pl.BlockSpec(memory_space=pl.ANY),
          ],
          out_specs=pl.BlockSpec(memory_space=pl.ANY),
          out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
      )(x)

  def test_dma_with_scalar_semaphore_ref(self):
    def kernel(x_hbm_ref, y_hbm_ref):
      def body(sem):
        pltpu.async_copy(x_hbm_ref.at[pl.ds(8), :], y_hbm_ref.at[:, pl.ds(128)],
                         sem.at[0]).wait()
      pl.run_scoped(body, pltpu.SemaphoreType.DMA((1,)))
    x = jnp.arange(8 * 128.).reshape((8, 128))

    y = self.pallas_call(
        kernel,
        in_specs=[
            pl.BlockSpec(memory_space=pl.ANY),
        ],
        out_specs=pl.BlockSpec(memory_space=pl.ANY),
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )(x)
    np.testing.assert_array_equal(y, x)

  def test_output_dma_semaphore_ref(self):
    if self.INTERPRET:
      self.skipTest('TODO(sharadmv, justinfu): Add interpret support for DMA.')

    def kernel(x_hbm_ref, y_hbm_ref, sem_out):
      pltpu.make_async_copy(
          x_hbm_ref.at[pl.ds(8), :], y_hbm_ref.at[:, pl.ds(128)], sem_out
      ).start()

    def kernel2(x_hbm_ref, y_hbm_ref, sem_in, y_hbm_out):
      del y_hbm_out
      pltpu.make_async_copy(
          x_hbm_ref.at[pl.ds(8), :], y_hbm_ref.at[:, pl.ds(128)], sem_in
      ).wait()

    x = jnp.arange(8 * 128.0).reshape((8, 128))

    @jax.jit
    def body(x):
      y, sem_out = self.pallas_call(
          kernel,
          in_specs=[
              pl.BlockSpec(memory_space=pl.ANY),
          ],
          out_specs=[
              pl.BlockSpec(memory_space=pl.ANY),
              pl.BlockSpec(memory_space=pltpu.SEMAPHORE),
          ],
          out_shape=[
              jax.ShapeDtypeStruct((8, 128), jnp.float32),
              pltpu.SemaphoreType.DMA,
          ],
      )(x)

      y = self.pallas_call(
          kernel2,
          in_specs=[
              pl.BlockSpec(memory_space=pl.ANY),
              pl.BlockSpec(memory_space=pl.ANY),
              pl.BlockSpec(memory_space=pltpu.SEMAPHORE),
          ],
          out_specs=pl.BlockSpec(memory_space=pl.ANY),
          out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
          input_output_aliases={1: 0},
      )(x, y, sem_out)
      return y

    np.testing.assert_array_equal(body(x), x)

  def test_hbm_hbm_grid_dma(self):
    # When using the grid, we have to emit Mosaic window_params. Test that they
    # work correctly with ANY memory space operands.
    def kernel(x_hbm_ref, y_hbm_ref):
      i = pl.program_id(0)
      def body(sem):
        pltpu.async_copy(
            x_hbm_ref.at[pl.ds(i, 1)], y_hbm_ref.at[pl.ds(i, 1)], sem
        ).wait()
      pl.run_scoped(body, pltpu.SemaphoreType.DMA)
    x = jnp.arange(2 * 8 * 128.).reshape((2, 8, 128))
    y = self.pallas_call(
        kernel,
        in_specs=[
            pl.BlockSpec(memory_space=pl.ANY),
        ],
        out_specs=pl.BlockSpec(memory_space=pl.ANY),
        out_shape=jax.ShapeDtypeStruct((2, 8, 128), jnp.float32),
        grid=(2,),
    )(x)
    np.testing.assert_allclose(y, x)

  def test_hbm_vmem_dma(self):
    def kernel(x_hbm_ref, y_ref):
      def body(x_ref, sem):
        pltpu.async_copy(x_hbm_ref.at[pl.ds(8), :], x_ref.at[:, pl.ds(128)],
                         sem).wait()
        y_ref[...] = x_ref[...]
      pl.run_scoped(
          body, pltpu.VMEM((8, 128), jnp.float32), pltpu.SemaphoreType.DMA
      )
    x = jnp.arange(8 * 128.).reshape((8, 128))
    y = self.pallas_call(
        kernel,
        in_specs=[
            pl.BlockSpec(memory_space=pl.ANY),
        ],
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )(x)
    np.testing.assert_allclose(y, x)

  def test_vmem_hbm_dma(self):
    def kernel(x_ref, y_hbm_ref):
      def body(y_ref, sem):
        y_ref[...] = x_ref[...]
        pltpu.async_copy(y_ref, y_hbm_ref, sem).wait()
      pl.run_scoped(
          body, pltpu.VMEM((8, 128), jnp.float32), pltpu.SemaphoreType.DMA
      )
    x = jnp.arange(8 * 128.).reshape((8, 128))
    y = self.pallas_call(
        kernel,
        out_specs=pl.BlockSpec(memory_space=pl.ANY),
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )(x)
    np.testing.assert_allclose(y, x)

  def test_vmem_hbm_vmem_dma(self):
    def kernel(x_hbm_ref, y_hbm_ref):
      def body(x_ref, y_ref, sem):
        pltpu.async_copy(x_hbm_ref, x_ref, sem).wait()
        y_ref[...] = x_ref[...]
        pltpu.async_copy(y_ref, y_hbm_ref, sem).wait()
      pl.run_scoped(
          body,
          pltpu.VMEM((8, 128), jnp.float32),
          pltpu.VMEM((8, 128), jnp.float32),
          pltpu.SemaphoreType.DMA,
      )
    x = jnp.arange(8 * 128.).reshape((8, 128))
    y = self.pallas_call(
        kernel,
        in_specs=[pl.BlockSpec(memory_space=pl.ANY)],
        out_specs=pl.BlockSpec(memory_space=pl.ANY),
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )(x)
    np.testing.assert_allclose(y, x)

  def test_hbm_smem_dma(self):
    def kernel(x_hbm_ref, y_ref):
      def body(x_ref, sem):
        pltpu.async_copy(x_hbm_ref, x_ref, sem).wait()
        y_ref[...] = x_ref[0, 0] * jnp.ones_like(y_ref)
      pl.run_scoped(
          body, pltpu.SMEM((8, 128), jnp.float32), pltpu.SemaphoreType.DMA
      )
    x = 4 * jnp.ones((8, 128), jnp.float32)
    y = self.pallas_call(
        kernel,
        in_specs=[
            pl.BlockSpec(memory_space=pl.ANY),
        ],
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )(x)
    np.testing.assert_allclose(y, x)

  def test_smem_hbm_dma(self):
    def kernel(x_ref, y_hbm_ref):
      def body(y_ref, sem):
        y_ref[0, 0] = 0.0
        y_ref[0, 1] = x_ref[4, 4]
        pltpu.async_copy(y_ref, y_hbm_ref, sem).wait()
      pl.run_scoped(
          body, pltpu.SMEM((1, 2), jnp.float32), pltpu.SemaphoreType.DMA
      )
    x = jnp.arange(8 * 128.).reshape((8, 128))
    y = self.pallas_call(
        kernel,
        in_specs=[
            pl.BlockSpec(memory_space=pltpu.SMEM),
        ],
        out_specs=pl.BlockSpec(memory_space=pl.ANY),
        out_shape=jax.ShapeDtypeStruct((1, 2), jnp.float32),
    )(x)
    expected = jnp.zeros_like(x[0:1, 0:2]).at[0, 1].set(x[4, 4])
    np.testing.assert_allclose(y, expected)

  def test_vmem_vmem_dma(self):
    def kernel(x_ref, y_ref):
      def body(sem):
        pltpu.async_copy(x_ref, y_ref, sem).wait()
      pl.run_scoped(body, pltpu.SemaphoreType.DMA)
    x = jnp.arange(8 * 128.).reshape((8, 128))
    y = self.pallas_call(
        kernel,
        in_specs=[
            pl.BlockSpec(memory_space=pltpu.VMEM),
        ],
        out_specs=pl.BlockSpec(memory_space=pltpu.VMEM),
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )(x)
    np.testing.assert_allclose(y, x)

  def test_hbm_vmem_dma_slicing(self):
    def kernel(x_hbm_ref, y_ref):
      def body(sem):
        dma1 = pltpu.async_copy(
            x_hbm_ref.at[pl.ds(0, 8)], y_ref.at[pl.ds(0, 8)], sem
        )
        dma2 = pltpu.async_copy(
            x_hbm_ref.at[pl.ds(8, 8)], y_ref.at[pl.ds(8, 8)], sem
        )
        dma1.wait()
        dma2.wait()
      pl.run_scoped(body, pltpu.SemaphoreType.DMA)
    x = jnp.arange(2 * 8 * 128.).reshape((16, 128))
    y = self.pallas_call(
        kernel,
        in_specs=[
            pl.BlockSpec(memory_space=pl.ANY),
        ],
        out_specs=pl.BlockSpec(memory_space=pltpu.VMEM),
        out_shape=jax.ShapeDtypeStruct((16, 128), jnp.float32),
    )(x)
    np.testing.assert_allclose(y, x)

  def test_hbm_vmem_dma_indexing(self):
    def kernel(x_hbm_ref, y_ref):
      def body(sem):
        dma1 = pltpu.async_copy(
            x_hbm_ref.at[0], y_ref.at[pl.ds(0, 8)], sem
        )
        dma2 = pltpu.async_copy(
            x_hbm_ref.at[1], y_ref.at[pl.ds(8, 8)], sem
        )
        dma1.wait()
        dma2.wait()
      pl.run_scoped(body, pltpu.SemaphoreType.DMA)
    x = jnp.arange(2 * 8 * 128.).reshape((2, 8, 128))
    y = self.pallas_call(
        kernel,
        in_specs=[
            pl.BlockSpec(memory_space=pl.ANY),
        ],
        out_specs=pl.BlockSpec(memory_space=pltpu.VMEM),
        out_shape=jax.ShapeDtypeStruct((16, 128), jnp.float32),
    )(x)
    np.testing.assert_allclose(y, x.reshape((16, 128)))

  def test_hbm_vmem_dma_multiple_indexing(self):
    if self.INTERPRET:
      self.skipTest('Multiple indexing not supported in interpret mode.')

    def kernel(x_hbm_ref, y_ref):
      def body(sem):
        for i in range(3):
          dma1 = pltpu.async_copy(
              x_hbm_ref.at[pl.ds(i, 1)].at[0, 0], y_ref.at[i].at[pl.ds(0, 8)],
              sem
          )
          dma2 = pltpu.async_copy(
              x_hbm_ref.at[pl.ds(i, 1)].at[0, 1], y_ref.at[i].at[pl.ds(8, 8)],
              sem
          )
          dma1.wait()
          dma2.wait()
      pl.run_scoped(body, pltpu.SemaphoreType.DMA)
    x = jnp.arange(3 * 2 * 8 * 128.).reshape((3, 2, 8, 128))
    y = self.pallas_call(
        kernel,
        in_specs=[
            pl.BlockSpec(memory_space=pl.ANY),
        ],
        out_specs=pl.BlockSpec(memory_space=pltpu.VMEM),
        out_shape=jax.ShapeDtypeStruct((3, 16, 128), jnp.float32),
    )(x)
    np.testing.assert_allclose(y, x.reshape((3, 16, 128)))

  def test_cannot_squeeze_lane_sublane(self):
    if self.INTERPRET:
      self.skipTest('Only works on Mosaic TPU.')

    def kernel(x_hbm_ref, y_ref):
      def body(sem):
        dma1 = pltpu.async_copy(
            x_hbm_ref.at[:, :, 0], y_ref.at[pl.ds(0, 8)], sem
        )
        dma2 = pltpu.async_copy(
            x_hbm_ref.at[:, :, 1], y_ref.at[pl.ds(8, 8)], sem
        )
        dma1.wait()
        dma2.wait()
      pl.run_scoped(body, pltpu.SemaphoreType.DMA)
    x = jnp.arange(2 * 8 * 128.).reshape((2, 8, 128))
    with self.assertRaises(Exception):
      _ = self.pallas_call(
          kernel,
          in_specs=[
              pl.BlockSpec(memory_space=pl.ANY),
          ],
          out_specs=pl.BlockSpec(memory_space=pltpu.VMEM),
          out_shape=jax.ShapeDtypeStruct((16, 128), jnp.float32),
      )(x)

  def test_hoisted_scratch_space(self):
    def kernel(x_ref, y_ref, scratch_ref):
      i = pl.program_id(0)
      @pl.when(i == 0)
      def _():
        scratch_ref[...] = x_ref[...]
      scratch_ref[...] += jnp.ones_like(scratch_ref)

      @pl.when(i == 2)
      def _():
        y_ref[...] = scratch_ref[...]

    x = jnp.arange(8 * 128.).reshape((8, 128))
    y = self.pallas_call(
        kernel,
        grid_spec=pltpu.PrefetchScalarGridSpec(
            num_scalar_prefetch=0,
            in_specs=[
                pl.BlockSpec((8, 128), lambda i: (0, 0)),
            ],
            scratch_shapes=[pltpu.VMEM((8, 128), jnp.float32)],
            out_specs=pl.BlockSpec((8, 128), lambda i: (0, 0)),
            grid=(3,),
        ),
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )(x)
    np.testing.assert_array_equal(y, x + 3)

  def test_hoisted_smem_space(self):
    # TODO(sharadmv,apaszke): enable SMEM scratch spaces
    # TODO(sharadmv,apaszke): add support for ()-shaped SMEM refs
    self.skipTest('Currently doesn\'t work')
    def kernel(y_ref, scratch_ref):
      scratch_ref[0, 0] = pl.program_id(0)
      y_ref[...] = jnp.broadcast_to(scratch_ref[0, 0], y_ref.shape)

    y = pl.pallas_call(
        kernel,
        grid_spec=pltpu.PrefetchScalarGridSpec(
            num_scalar_prefetch=0,
            in_specs=[],
            scratch_shapes=[pltpu.SMEM((1, 1), jnp.int32)],
            out_specs=pl.BlockSpec((None, 8, 128), lambda i: (i, 0, 0)),
            grid=(2,),
        ),
        out_shape=jax.ShapeDtypeStruct((2, 8, 128), jnp.int32),
    )()
    expected = jnp.broadcast_to(jnp.arange(2, dtype=jnp.int32)[..., None, None],
                                (2, 8, 128))
    np.testing.assert_array_equal(y, expected)

  def test_hoisted_semaphore(self):
    def kernel(x_bbm_ref, y_ref, sem, dma_sem):
      pltpu.semaphore_signal(sem)
      pltpu.semaphore_wait(sem)
      pltpu.async_copy(x_bbm_ref, y_ref, dma_sem).wait()

    x = jnp.arange(8 * 128.).reshape((8, 128))
    y = self.pallas_call(
        kernel,
        grid_spec=pltpu.PrefetchScalarGridSpec(
            num_scalar_prefetch=0,
            in_specs=[
                pl.BlockSpec(memory_space=pl.ANY),
            ],
            scratch_shapes=[pltpu.SemaphoreType.REGULAR,
                            pltpu.SemaphoreType.DMA],
            out_specs=pl.BlockSpec(memory_space=pltpu.VMEM),
        ),
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )(x)
    np.testing.assert_array_equal(y, x)

  @jtu.thread_unsafe_test()  # Uses a lot of TPU memory.
  def test_large_array_indexing(self):
    n = 6
    dtype = jnp.bfloat16
    # This test sometimes OOMs on smaller chips. We garbage collect
    # to increase the chance there is 6GB memory available.
    gc.collect()
    x = jax.lax.broadcasted_iota(dtype, (n, 1024 * 1024, 512), 0)

    def kernel(index, x, y, sem):
      pltpu.async_copy(x.at[index[0]], y.at[:], sem).wait()

    run = self.pallas_call(kernel,
                         grid_spec=pltpu.PrefetchScalarGridSpec(
                             num_scalar_prefetch=1,
                             in_specs=[
                                 pl.BlockSpec(
                                     memory_space=pl.ANY)],
                             out_specs=pl.BlockSpec(
                                 memory_space=pl.ANY),
                             scratch_shapes=[pltpu.SemaphoreType.DMA],
                             ),
                         out_shape=jax.ShapeDtypeStruct(x.shape[1:], dtype),
                         )

    for i in range(x.shape[0]):
      y = run(jnp.array([i], dtype=jnp.int32), x)
      np.testing.assert_array_equal(y, i)
      del y

  def test_dynamic_dma_on_2nd_minor(self):
    def kernel(array, data, index, size, _, sem):
      pltpu.async_copy(
            data.at[pl.ds(0, size[0])], array.at[pl.ds(index[0], size[0])], sem
        ).wait()

    def run(array, data, index, size):
      return pl.pallas_call(
            kernel,
            out_shape=array,
            in_specs=[
                pl.BlockSpec(memory_space=pltpu.ANY),
                pl.BlockSpec(memory_space=pltpu.VMEM),
                pl.BlockSpec(memory_space=pltpu.SMEM),
                pl.BlockSpec(memory_space=pltpu.SMEM),
            ],
            scratch_shapes=[
                pltpu.SemaphoreType.DMA,
            ],
            out_specs=pl.BlockSpec(memory_space=pltpu.ANY),
            input_output_aliases={0: 0},
        )(array, data, index, size)

    array = jnp.zeros((1024, 128), jnp.int32)
    data = jnp.ones((8, 128), jnp.int32)
    index = jnp.array([3], jnp.int32)
    size = jnp.array([5], jnp.int32)

    expected = array.at[index[0] : index[0] + size[0]].set(
        data[index[0] : index[0] + size[0]]
    )
    result = run(array, data, index, size)
    np.testing.assert_array_equal(result, expected)


class PallasCallDMAInterpretTest(PallasCallDMATest):
  INTERPRET = True

  def test_interpret_local_dma(self):
    # We run this test in interpret mode to test semaphore counting.
    # On a physical device the values update asynchronously so we cannot
    # deterministically check the values.
    def test_kernel(x_ref,
                o_ref,
                sem_out_ref,
                copy_sem,
                ):
      o_ref[...] = jnp.zeros_like(o_ref[...])
      input_to_output_copy = pltpu.make_async_copy(
          src_ref=x_ref.at[0:8],
          dst_ref=o_ref.at[0:8],
          sem=copy_sem.at[0],
      )
      input_to_output_copy.start()
      sem_out_ref[0, :] = jnp.ones_like(
          sem_out_ref[0, :]) * pltpu.semaphore_read(copy_sem.at[0])
      input_to_output_copy.wait()
      sem_out_ref[1, :] = jnp.ones_like(
          sem_out_ref[0, :]) * pltpu.semaphore_read(copy_sem.at[0])

    out_shape = (jax.ShapeDtypeStruct((16, 128), jnp.int32),
                 jax.ShapeDtypeStruct((2, 1), jnp.int32))
    grid_spec = pltpu.PrefetchScalarGridSpec(
            num_scalar_prefetch=0,
            in_specs=[
                pl.BlockSpec(memory_space=pl.ANY),
            ],
            scratch_shapes=(
                [pltpu.SemaphoreType.DMA(2,)]
            )
        )

    kernel = pl.pallas_call(
        test_kernel,
        out_shape=out_shape,
        grid_spec=grid_spec,
        interpret=True,
    )
    x = jax.random.randint(
        jax.random.key(0), shape=(16, 128), minval=0, maxval=128)

    result, semaphores = kernel(x)
    np.testing.assert_array_equal(result[0:8], x[0:8])
    np.testing.assert_array_equal(result[8:], jnp.zeros_like(result[8:]))

    # Make sure semaphores have the correct value before and after DMA wait.
    result_sem_pre_wait = semaphores[0, 0]
    np.testing.assert_array_equal(result_sem_pre_wait, result[0:8].size)
    result_sem_post_wait = semaphores[1, 0]
    np.testing.assert_array_equal(result_sem_post_wait, 0)

  def test_interpreter_semaphore_counting(self):
    # We run this test in interpret mode because the kernel exits with
    # non-zero values. In normal Pallas this would crash the kernel.
    def test_kernel(o_ref,
                    sem_ref,
                ):
      o_ref[...] = jnp.zeros_like(o_ref)
      pltpu.semaphore_signal(sem_ref.at[0], 1)
      pltpu.semaphore_signal(sem_ref.at[1], 2)
      pltpu.semaphore_signal(sem_ref.at[2], 3)
      pltpu.semaphore_signal(sem_ref.at[3], 4)
      o_ref[0, 0] = pltpu.semaphore_read(sem_ref.at[0])
      o_ref[1, 0] = pltpu.semaphore_read(sem_ref.at[1])
      o_ref[2, 0] = pltpu.semaphore_read(sem_ref.at[2])
      o_ref[3, 0] = pltpu.semaphore_read(sem_ref.at[3])
      pltpu.semaphore_wait(sem_ref.at[0], 4)
      pltpu.semaphore_wait(sem_ref.at[1], 3)
      pltpu.semaphore_wait(sem_ref.at[2], 2)
      pltpu.semaphore_wait(sem_ref.at[3], 1)
      o_ref[4, 0] = pltpu.semaphore_read(sem_ref.at[0])
      o_ref[5, 0] = pltpu.semaphore_read(sem_ref.at[1])
      o_ref[6, 0] = pltpu.semaphore_read(sem_ref.at[2])
      o_ref[7, 0] = pltpu.semaphore_read(sem_ref.at[3])

    out_shape = jax.ShapeDtypeStruct((8, 1), jnp.int32)
    grid_spec = pltpu.PrefetchScalarGridSpec(
            num_scalar_prefetch=0,
            scratch_shapes=(
                [pltpu.SemaphoreType.REGULAR(4,)]
            )
        )
    results = pl.pallas_call(
        test_kernel,
        out_shape=out_shape,
        grid_spec=grid_spec,
        interpret=True,
    )()
    expected = jnp.array([1, 2, 3, 4, -3, -1, 1, 3]).reshape(out_shape.shape)
    np.testing.assert_array_equal(results, expected)


class PallasCallTest(PallasBaseTest):

  @parameterized.parameters([
      dict(shape=shape, dty=dty)
      for shape, dty in itertools.product(
          [(4, 2, 9), (1, 1025), (1024, 1024)], [jnp.float32, jnp.int32]
      )
  ])
  def test_double_replicated_reduction(self, shape, dty):
    if not jtu.if_cloud_tpu_at_least(2025, 2, 19):
      self.skipTest("Needs a newer libTPU")
    def body(o_ref):
      x = jnp.full(shape, 2.0, dtype=dty)
      reduction = jnp.sum(x, axis=None)
      bcast = jnp.full((vregs_in_block * 1024,), reduction)
      o_ref[:] = bcast

    vregs_in_block = 2
    total_vregs = 4

    data_size = total_vregs * 1024
    block_size = vregs_in_block * 1024

    @jax.jit
    def reduce():
      return self.pallas_call(
        body,
        out_shape=jax.ShapeDtypeStruct((data_size,), dty),
        in_specs=[],
        out_specs=pl.BlockSpec((block_size,), lambda i: i),
        grid= data_size // block_size,
    )()

    x = jnp.full(shape, 2.0, dtype=dty)
    z = jax.block_until_ready(reduce())
    reduce_value = jnp.sum(jnp.full(shape, x), dtype=dty)
    np.testing.assert_allclose(z, reduce_value)

  def test_scalar_any_input(self):
    if not jtu.is_device_tpu_at_least(4):
      self.skipTest("Needs a newer TPU")
    if not jtu.if_cloud_tpu_at_least(2025, 5, 1):
      self.skipTest("Needs a newer libTPU")
    def kernel(src, dst, sem):
      pltpu.async_copy(src, dst, sem).wait()

    def run(src):
      return pl.pallas_call(
          kernel,
          out_shape=jax.ShapeDtypeStruct(src.shape, jnp.float32),
          in_specs=[pl.BlockSpec(memory_space=pltpu.ANY)],
          scratch_shapes=[pltpu.SemaphoreType.DMA],
          out_specs=pl.BlockSpec(memory_space=pltpu.SMEM),
      )(src)
    x = jnp.full((1,), 3.1415, dtype=jnp.float32)
    np.testing.assert_array_equal(run(x), x)

  def test_sum_in_smem(self):
    if not jtu.if_cloud_tpu_at_least(2025, 4, 30):
      self.skipTest("Needs a newer libTPU")
    def kernel(x, out):
      a = jnp.array(0, dtype=jnp.int32)
      for i in range(4):
        for j in range(4):
          out[i, j] = a.astype(out.dtype)
          a += x[i, j].astype(jnp.int32)

    x = jnp.ones((4, 4), jnp.int16)
    spec = pl.BlockSpec(memory_space=pltpu.SMEM)
    y = pl.pallas_call(kernel, in_specs=[spec], out_specs=spec, out_shape=x)(x)
    np.testing.assert_array_equal(
        y, jnp.arange(16, dtype=jnp.int32).reshape(4, 4)
    )

  @parameterized.parameters([
      dict(
          m=m,
          replicated=replicated,
          reduced_dims=reduced_dims,
          dty=dty,
          reduce_func=reduce_func,
      )
      for m, replicated, reduced_dims, dty, reduce_func in itertools.product(
          [128, 256],
          [(True, True), (False, True), (True, False)],
          [(0, 1), (0,), (1,)],
          [jnp.float32, jnp.int32],
          [jnp.sum, jnp.max, jnp.min],
      )
  ])
  def test_replicated_broadcast_reduction(
      self, m, replicated, reduced_dims, dty, reduce_func
  ):
    if not jtu.if_cloud_tpu_at_least(2025, 2, 19):
      self.skipTest("Needs a newer libTPU")
    if dty == jnp.int32 and 1 in reduced_dims:
      # TODO(b/395579834): Remove this skip once we implement this.
      self.skipTest('int32 reduction on last dimension not supported')
    if not jtu.is_device_tpu_at_least(4) and len(replicated) == 2:
      self.skipTest(
          'Brodcast in both sublanes and lanes not supported on this hardware'
      )

    in_shape = (1 if replicated[0] else m, 1 if replicated[1] else m)
    red_shape = [m, m]
    for d in reduced_dims:
      red_shape[d] = 1

    def body(x_ref, o_ref):
      x = x_ref[:]
      dilated_x = jnp.broadcast_to(x, (m, m))
      reduced = reduce_func(dilated_x, axis=reduced_dims).reshape(red_shape)
      o_ref[:] = reduced

    @jax.jit
    def reduce(x):
      return self.pallas_call(
        body,
        out_shape=jax.ShapeDtypeStruct(red_shape, dty),
        in_specs=[pl.BlockSpec(in_shape)],
        out_specs=pl.BlockSpec(red_shape),
        grid=1,
    )(x)

    x = jnp.full(in_shape, 2.0, dtype=dty)
    y = jax.block_until_ready(reduce(x))
    dilated_x = jnp.broadcast_to(x, (m, m))
    expected = reduce_func(dilated_x, axis=reduced_dims).reshape(red_shape)
    np.testing.assert_allclose(y, expected)

  def test_cost_analysis(self):
    def kernel(x, y):
      y[:] = x[:]
    x = jnp.arange(1024.).reshape(8, 128)
    f = self.pallas_call(
        kernel,
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
        cost_estimate=pl.CostEstimate(
            flops=1234, transcendentals=21, bytes_accessed=12345
        ),
    )
    analysis_result = jax.jit(f).lower(x).compile().cost_analysis()
    self.assertEqual(analysis_result['flops'], 1234)
    self.assertEqual(analysis_result['transcendentals'], 21)
    self.assertEqual(analysis_result['bytes accessed'], 12345)

  def test_cost_analysis_vmap(self):
    def kernel(x, y):
      y[:] = x[:]
    batch_size = 3
    x = jnp.arange(batch_size * 1024.).reshape(batch_size, 8, 128)
    f = pl.pallas_call(
        kernel,
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
        cost_estimate=pl.CostEstimate(
            flops=1234, transcendentals=21, bytes_accessed=12345
        ),
    )
    f = jax.vmap(f)
    analysis_result = jax.jit(f).lower(x).compile().cost_analysis()
    self.assertEqual(analysis_result['flops'], batch_size * 1234)
    self.assertEqual(analysis_result['transcendentals'], batch_size * 21)
    self.assertEqual(analysis_result['bytes accessed'], batch_size * 12345)

  def test_vmem_limit(self):
    shape = (128, 128)

    def kernel(x_ref, y_ref):
      y_ref[...] = x_ref[...]

    x = jnp.arange(np.prod(shape), dtype=np.float32).reshape(shape)
    with self.assertRaises(_jax.XlaRuntimeError):
      self.pallas_call(
          kernel,
          out_shape=x,
          compiler_params=pltpu.CompilerParams(vmem_limit_bytes=256),
      )(x)
    self.pallas_call(
        kernel,
        out_shape=x,
        compiler_params=pltpu.CompilerParams(vmem_limit_bytes=int(2**18)),
    )(x)

  def test_allow_input_fusion(self):
    shape = (3, 128, 128)

    def kernel(x_ref, y_ref):
      y_ref[...] = x_ref[...]

    def f(x, y):
      z = jax.numpy.add(x, y)
      return self.pallas_call(
          kernel,
          grid=(3,),
          in_specs=[pl.BlockSpec((1, 128, 128), lambda i: (i, 0, 0))],
          out_specs=pl.BlockSpec((1, 128, 128), lambda i: (i, 0, 0)),
          out_shape=x,
          compiler_params=pltpu.CompilerParams(allow_input_fusion=[True]),
      )(z)

    x = jnp.arange(np.prod(shape), dtype=np.float32).reshape(shape)
    y = jnp.arange(np.prod(shape), dtype=np.float32).reshape(shape)

    out = f(x, y)
    expected = x + y
    np.testing.assert_array_equal(out, expected)
    compiled = jax.jit(f).lower(x, y).compile().as_text()
    assert re.search(r'fusion.*kind=kCustom.*fused_computation', compiled)

  def test_set_internal_scratch_size(self):
    shape = (128, 128)

    def kernel(x_ref, y_ref):
      y_ref[...] = x_ref[...]

    x = np.arange(np.prod(shape), dtype=np.float32).reshape(shape)
    requested_bytes = 128 * 4
    with self.assertRaisesRegex(
        Exception,
        f'Requested internal scratch size {requested_bytes} needs to be at'
        ' least',
    ):
      self.pallas_call(
          kernel,
          out_shape=jax.ShapeDtypeStruct(shape, jnp.float32),
          compiler_params=pltpu.CompilerParams(
              internal_scratch_in_bytes=requested_bytes,
          ),
      )(x)

  @parameterized.product(
    dtype=[jnp.bfloat16, jnp.float32],
    axis=[1, -1],
  )
  def test_pltpu_repeat(self, dtype, axis):
    def test_kernel(x_ref, o_ref):
      x = x_ref[...]
      o_ref[...] = pltpu.repeat(x, 2, axis=axis)

    @jax.jit
    def test(x: jax.Array) -> jax.Array:
      return pl.pallas_call(
          test_kernel,
          out_shape=jax.ShapeDtypeStruct([x.shape[0], x.shape[1] * 2], x.dtype),
      )(x)

    x = jnp.arange(2048, dtype=dtype).reshape((8, 256))
    y = test(x)
    np.testing.assert_array_equal(y, jnp.concatenate([x, x], axis=1))

  def test_mixed_precision_dot(self):
    if not jtu.if_cloud_tpu_at_least(2025, 2, 27):
      self.skipTest("Needs a newer libTPU")

    if not jtu.is_device_tpu_at_least(5):
      self.skipTest('float8_e4m3b11fnuz not supported on TPU generations <= 4')

    def kernel(x_ref, w_ref, o_ref):
      o_ref[:] = jax.lax.dot_general(
          x_ref[:],
          w_ref[:],
          dimension_numbers=(((1,), (0,)), ((), ())),
          preferred_element_type=jnp.float32,
      )

    x = jnp.ones((64, 128), dtype=jnp.bfloat16)
    w = jnp.full((128, 128), jnp.nan, jnp.float8_e4m3b11fnuz)

    run = pl.pallas_call(kernel, jax.ShapeDtypeStruct((64, 128), jnp.float32))
    run = jax.named_call(run, name='run')
    run = jax.jit(run)

    expected = jax.lax.dot_general(
        x,
        w,
        dimension_numbers=(((1,), (0,)), ((), ())),
        preferred_element_type=jnp.float32,
    )
    jax_nans = jnp.isnan(expected).sum()
    mosaic_nans = jnp.isnan(run(x, w)).sum()
    self.assertEqual(jax_nans, mosaic_nans)

  @parameterized.product(in_dtype=[jnp.int4, jnp.int8, jnp.int16, jnp.int32])
  def test_scalar_load_upcast(self, in_dtype):
    if not jtu.if_cloud_tpu_at_least(2025, 4, 25):
      self.skipTest("Needs a newer libTPU")
    if in_dtype == jnp.int4 and not jtu.is_device_tpu_at_least(4):
      self.skipTest("Triggers an XLA bug")  # TODO(b/413602952)
    def kernel(x_ref, o_ref):
      o_ref[0, 0] = x_ref[0, 0].astype(o_ref.dtype)
    x = jnp.asarray([[-1]], dtype=in_dtype)
    y = pl.pallas_call(
        kernel,
        in_specs=[pl.BlockSpec(memory_space=pltpu.SMEM)],
        out_specs=pl.BlockSpec(memory_space=pltpu.SMEM),
        out_shape=jax.ShapeDtypeStruct((1, 1), jnp.int32),
    )(x)
    self.assertEqual(y, x.astype(jnp.int32))

  @parameterized.product(in_dtype=[jnp.int4, jnp.int8, jnp.int16, jnp.int32])
  def test_scalar_indirect_load(self, in_dtype):
    if not jtu.if_cloud_tpu_at_least(2025, 4, 27):
      self.skipTest("Needs a newer libTPU")
    def kernel(x_ref, o_ref):
      o_ref[0, 0] = x_ref[0, x_ref[0, 0].astype(jnp.int32)].astype(o_ref.dtype)
    if in_dtype == jnp.int4 and not jtu.is_device_tpu_at_least(4):
      self.skipTest("Triggers an XLA bug")  # TODO(b/413602952)
    x = jnp.asarray([[3, 0, 0, 1]], dtype=in_dtype)
    y = pl.pallas_call(
        kernel,
        in_specs=[pl.BlockSpec(memory_space=pltpu.SMEM)],
        out_specs=pl.BlockSpec(memory_space=pltpu.SMEM),
        out_shape=jax.ShapeDtypeStruct((1, 1), jnp.int32),
    )(x)
    self.assertEqual(y, x[0, x[0, 0]].astype(jnp.int32)[None, None])

  def test_masked_store(self):
    shape = (16, 256)
    mask_shape = (10, 130)
    mask_start = (4, 5)
    dtype = jnp.float32
    def body(scalar_ref, x_ref, o_ref):
      o_ref[...] = jnp.full(shape, -1, dtype=dtype)
      b0, b1 = scalar_ref[0], scalar_ref[1]
      e0, e1 = b0 + mask_shape[0], b1 + mask_shape[1]
      iota0 = lax.broadcasted_iota(jnp.int32, shape, 0)
      iota1 = lax.broadcasted_iota(jnp.int32, shape, 1)
      mask0 = jnp.logical_and(b0 <= iota0, iota0 < e0)
      mask1 = jnp.logical_and(b1 <= iota1, iota1 < e1)
      pl.store(
          o_ref,
          (slice(None), slice(None)),
          x_ref[...],
          mask=jnp.logical_and(mask0, mask1),
      )

    s = jnp.array(mask_start, jnp.int32)
    x = jnp.arange(np.prod(shape), dtype=dtype).reshape(shape)
    out = pl.pallas_call(
        body,
        out_shape=jax.ShapeDtypeStruct(shape, dtype),
        grid_spec=pltpu.PrefetchScalarGridSpec(
          num_scalar_prefetch=1,
        ),
    )(s, x)
    slices = tuple(slice(b, b + l) for b, l in zip(mask_start, mask_shape))
    expected = jnp.full(shape, -1, dtype=dtype)
    expected = expected.at[slices].set(x[slices])
    np.testing.assert_array_equal(out, expected)

  def test_custom_vjp(self):

    @jax.custom_vjp
    def f(x):
      return jnp.tanh(x)
    def f_fwd(x):
      return jnp.tanh(x) * 2, ()
    def f_bwd(_, g):
      return (g * 2,)

    f.defvjp(f_fwd, f_bwd)

    def kernel(x_ref, dy_ref, y_ref, y_p_ref, dx_ref):
      x = x_ref[...]
      y_ref[...] = f(x)
      y_p, f_vjp = jax.vjp(f, x)
      y_p_ref[...] = y_p
      dx_ref[...] = f_vjp(dy_ref[...])[0]

    x = jax.random.normal(jax.random.key(0), (8, 128), dtype=jnp.float32)
    dy = jax.random.normal(jax.random.key(1), (8, 128), dtype=jnp.float32)
    y, y_p, dx = pl.pallas_call(
        kernel,
        out_shape=(
            jax.ShapeDtypeStruct((8, 128), jnp.float32),
            jax.ShapeDtypeStruct((8, 128), jnp.float32),
            jax.ShapeDtypeStruct((8, 128), jnp.float32),
        ),
    )(x, dy)
    np.testing.assert_array_equal(y, f(x))
    np.testing.assert_array_equal(y_p, f(x) * 2)
    np.testing.assert_array_equal(dx, dy * 2)


class PallasUXTest(PallasBaseTest):

  def test_mlir_location(self):
    # Make sure that MLIR locations are correctly propagated to primitives.
    args = (jax.ShapeDtypeStruct((8, 128), jnp.float32),)
    f = example_kernel.double
    as_tpu_kernel = mosaic.as_tpu_kernel
    def capture_as_tpu_kernel(module, *args, **kwargs):
      asm = module.operation.get_asm(enable_debug_info=True)
      self.assertIn('example_kernel.py":25', asm)
      return as_tpu_kernel(module, *args, **kwargs)
    mosaic.as_tpu_kernel = capture_as_tpu_kernel
    try:
      jax.jit(f).lower(*args)
    finally:
      mosaic.as_tpu_kernel = as_tpu_kernel


class PallasMegacoreTest(PallasBaseTest):

  def test_megacore_splitting(self):
    # We want to make sure a 3-sized dimension is split across megacore
    # correctly, and if we combine the (3, 3) dimensions together it is still
    # correct.

    def matmul_kernel(x_ref, y_ref, z_ref):
      @pl.when(pl.program_id(2) == 0)
      def _():
        z_ref[...] = jnp.zeros_like(z_ref)
      z_ref[...] += x_ref[...] @ y_ref[...]

    k1, k2 = jax.random.split(jax.random.key(0))
    x = jax.random.uniform(k1, (3, 3, 512, 512))
    y = jax.random.uniform(k2, (3, 3, 512, 512))

    z = jax.vmap(
        jax.vmap(
            pl.pallas_call(
                matmul_kernel,
                out_shape=jax.ShapeDtypeStruct((512, 512), jnp.float32),
                grid=(4, 4, 4),
                in_specs=[
                    pl.BlockSpec((128, 128), lambda i, j, k: (i, k)),
                    pl.BlockSpec((128, 128), lambda i, j, k: (k, j)),
                ],
                out_specs=pl.BlockSpec((128, 128), lambda i, j, k: (i, j)),
            )
        )
    )(x, y)
    np.testing.assert_allclose(
        z, jax.vmap(jax.vmap(jnp.dot))(x, y), rtol=1e-6
    )


class PallasCallVmapTest(PallasBaseTest):

  def test_scratch_input_vmap(self):
    """Test that vmapp-ing a kernel with scratch inputs works correctly."""

    # Scratch inputs are only available for PallasTPU. This is why this test
    # does not live with the other vmap tests in:
    # jax/tests/pallas/pallas_test.py
    def add_one_with_scratch(x_ref, o_ref, scratch_ref):
      scratch_ref[...] = jnp.ones_like(scratch_ref[...])
      o_ref[...] = x_ref[...] + scratch_ref[...]

    tile_size = 128
    tile_shape = (tile_size, tile_size)
    array_shape = (2 * tile_size, 2 * tile_size)
    vmapped_add_one_with_scratch = jax.vmap(
        pl.pallas_call(
            add_one_with_scratch,
            out_shape=jax.ShapeDtypeStruct(array_shape, jnp.int32),
            grid_spec=pltpu.PrefetchScalarGridSpec(
                num_scalar_prefetch=0,
                in_specs=[pl.BlockSpec(tile_shape, lambda i, j: (i, j))],
                out_specs=pl.BlockSpec(tile_shape, lambda i, j: (i, j)),
                scratch_shapes=[pltpu.VMEM(tile_shape, dtype=jnp.int32)],
                grid=(2, 2),
            ),
        )
    )

    x = jnp.broadcast_to(jnp.arange(array_shape[0]), (10, *array_shape))

    out = vmapped_add_one_with_scratch(x)
    out_ref = x + 1

    np.testing.assert_array_equal(out, out_ref, strict=True)


class PallasCallDynamicDMATest(PallasBaseTest):

  def setUp(self):
    super().setUp()
    if not jtu.is_device_tpu_at_least(4):
      self.skipTest('DMAs not supported on TPU generations <= 3')

  def test_simple_tile_aligned_dynamic_size_dma(self):

    def kernel(size_smem_ref, x_hbm_ref, _, o_hbm_ref, sem):
      size = size_smem_ref[0]
      pltpu.async_copy(
          x_hbm_ref.at[pl.ds(0, size)],
          o_hbm_ref.at[pl.ds(0, size)], sem).wait()

    x = jnp.tile(jnp.arange(8, dtype=jnp.int32)[:, None, None], [1, 8, 128])
    o = jnp.zeros((8, 8, 128), dtype=jnp.int32)
    size = jnp.array([4], dtype=jnp.int32)

    out = pl.pallas_call(
        kernel,
        grid_spec=pltpu.PrefetchScalarGridSpec(
          num_scalar_prefetch=0,
          in_specs=[pl.BlockSpec(memory_space=pltpu.SMEM),
                    pl.BlockSpec(memory_space=pltpu.ANY),
                    pl.BlockSpec(memory_space=pltpu.ANY)],
          out_specs=pl.BlockSpec(memory_space=pltpu.ANY),
          scratch_shapes=[pltpu.SemaphoreType.DMA]
        ),
        out_shape=o,
        input_output_aliases={2: 0},
    )(size, x, o)
    expected = o.at[:4].set(x.at[:4].get())
    np.testing.assert_array_equal(out, expected)

  def test_simple_dynamic_size_dma(self):
    self.skipTest("doesn't work yet.")
    def kernel(size_smem_ref, x_hbm_ref, _, o_hbm_ref, sem):
      size = size_smem_ref[0]
      pltpu.async_copy(
          x_hbm_ref.at[pl.ds(0, size)],
          o_hbm_ref.at[pl.ds(0, size)], sem).wait()

    x = jnp.arange(8, dtype=jnp.int32)
    o = jnp.zeros(8, dtype=jnp.int32)
    size = jnp.array([4], dtype=jnp.int32)

    out = pl.pallas_call(
        kernel,
        grid_spec=pltpu.PrefetchScalarGridSpec(
          num_scalar_prefetch=0,
          in_specs=[pl.BlockSpec(memory_space=pltpu.SMEM),
                    pl.BlockSpec(memory_space=pltpu.ANY),
                    pl.BlockSpec(memory_space=pltpu.ANY)],
          out_specs=pl.BlockSpec(memory_space=pltpu.ANY),
          scratch_shapes=[pltpu.SemaphoreType.DMA]
        ),
        out_shape=o,
        input_output_aliases={2: 0},
    )(size, x, o)
    expected = o.at[:4].set(x.at[:4].get())
    np.testing.assert_array_equal(out, expected)


class PallasCallRefTransformTest(PallasBaseTest):

  @parameterized.product(slice_first=[True, False])
  def test_dma_bitcasted_ref(self, slice_first):
    if not jtu.is_device_tpu_at_least(4):
      self.skipTest('DMAs not supported on TPU generations <= 3')

    def kernel(x_hbm_ref, y_hbm_ref):
      def body(sem):
        ref = (
            x_hbm_ref.at[:8, :, :128].bitcast(jnp.int16)
            if slice_first
            else x_hbm_ref.bitcast(jnp.int16).at[:8, :, :128]
        )
        pltpu.async_copy(ref, y_hbm_ref.at[...], sem).wait()

      pl.run_scoped(body, pltpu.SemaphoreType.DMA)

    x = jnp.arange(4 * 8 * 128, dtype=jnp.int32).reshape((16, 1, 256))
    y = self.pallas_call(
        kernel,
        in_specs=[
            pl.BlockSpec(memory_space=pl.ANY),
        ],
        out_specs=pl.BlockSpec(memory_space=pl.ANY),
        out_shape=jax.ShapeDtypeStruct((8, 2, 128), jnp.int16),
    )(x)
    expected = (
        state_utils.bitcast(x[:8, :, :128], jnp.int16)
        if slice_first
        else state_utils.bitcast(x, jnp.int16)[:8, :, :128]
    )
    np.testing.assert_array_equal(y, expected)

  @parameterized.product(
      slice_first=[True, False], use_primitive_io_op=[False, True]
  )
  def test_load_bitcasted_ref(
      self, slice_first: bool, use_primitive_io_op: bool
  ):
    def kernel(x_ref, y_ref):
      ref = (
          x_ref.at[:8, :128].bitcast(jnp.int16)
          if slice_first
          else x_ref.bitcast(jnp.int16).at[:16, :128]
      )
      if use_primitive_io_op:
        pl.store(y_ref, ..., pl.load(ref, ...))
      else:
        y_ref[...] = ref[...]

    x = jnp.arange(4 * 8 * 128, dtype=jnp.int32).reshape((16, 256))
    y = self.pallas_call(
        kernel,
        out_shape=jax.ShapeDtypeStruct((16, 128), jnp.int16),
    )(x)
    expected = (
        state_utils.bitcast(x[:8, :128], jnp.int16)
        if slice_first
        else state_utils.bitcast(x, jnp.int16)[:16, :128]
    )
    np.testing.assert_array_equal(y, expected)

  @parameterized.product(
      slice_first=[True, False], use_primitive_io_op=[False, True]
  )
  def test_store_bitcasted_ref(
      self, slice_first: bool, use_primitive_io_op: bool
  ):
    def kernel(x_ref, y_ref):
      ref = (
          y_ref.at[:8, :128].bitcast(jnp.bfloat16)
          if slice_first
          else y_ref.bitcast(jnp.bfloat16).at[:16, :128]
      )
      if use_primitive_io_op:
        pl.store(ref, ..., pl.load(x_ref, ...))
      else:
        ref[...] = x_ref[...]

    x = jnp.arange(16 * 128, dtype=jnp.bfloat16).reshape((16, 128))
    y = self.pallas_call(
        kernel,
        out_shape=jax.ShapeDtypeStruct((16, 256), jnp.int32),
    )(x)
    expected = state_utils.bitcast(x, jnp.int32)
    np.testing.assert_array_equal(y[:8, :128], expected)

  @parameterized.product(slice_first=[True, False])
  def test_dma_reshaped_ref(self, slice_first):
    if not jtu.is_device_tpu_at_least(4):
      self.skipTest('DMAs not supported on TPU generations <= 3')

    def kernel(x_hbm_ref, y_hbm_ref):
      def body(sem):
        ref = (
            x_hbm_ref.at[:8, :, :].reshape(8, 128)
            if slice_first
            else x_hbm_ref.reshape(16, 128).at[:8, :]
        )
        pltpu.async_copy(ref, y_hbm_ref.reshape(8, 128).at[...], sem).wait()

      pl.run_scoped(body, pltpu.SemaphoreType.DMA)

    x = jnp.arange(16 * 128, dtype=jnp.int32).reshape(16, 1, 128)
    y = self.pallas_call(
        kernel,
        in_specs=[
            pl.BlockSpec(memory_space=pl.ANY),
        ],
        out_specs=pl.BlockSpec(memory_space=pl.ANY),
        out_shape=jax.ShapeDtypeStruct((8, 1, 128), jnp.int32),
    )(x)
    expected = (
        x[:8, :, :128].reshape((8, 128))
        if slice_first
        else x.reshape(16, 128)[:8, :128]
    ).reshape(8, 1, 128)
    np.testing.assert_array_equal(y, expected)

  def test_load_reshaped_ref(self):
    if not jtu.is_device_tpu_at_least(4):
      self.skipTest('No expected (1, 128) tiling')

    def kernel(x_ref, y_ref):
      y_ref[...] = x_ref.reshape(5, 128)[...]

    x = jnp.arange(5 * 128, dtype=jnp.int32).reshape(5, 1, 128)
    y = self.pallas_call(
        kernel,
        out_shape=jax.ShapeDtypeStruct((5, 128), jnp.int32),
    )(x)
    expected = x.reshape(5, 128)
    np.testing.assert_array_equal(y, expected)

  def test_store_reshaped_ref(self):
    if not jtu.is_device_tpu_at_least(4):
      self.skipTest('No expected (1, 128) tiling')

    def kernel(x_ref, y_ref):
      y_ref.reshape(5, 128)[...] = x_ref[...]

    x = jnp.arange(5 * 128, dtype=jnp.int32).reshape(5, 128)
    y = self.pallas_call(
        kernel,
        out_shape=jax.ShapeDtypeStruct((5, 1, 128), jnp.int32),
    )(x)
    expected = x.reshape(5, 1, 128)
    np.testing.assert_array_equal(y, expected)

  def test_multiple_ref_transforms(self):

    def kernel(x_ref, y_ref):
      ref = (
          x_ref.at[:16, :256]  # i32(16, 256)
          .bitcast(jnp.int16)  # i16(32, 256)
          .reshape((2, 16, 256))  # i16(2, 16, 256)
          .bitcast(jnp.float16)  # bf16(2, 16, 256)
          .at[1:, :, :]  # bf16(1, 16, 256)
          .reshape((16, 256))  # bf16(16, 256)
          .at[:, :128]  # bf16(16, 128)
          .bitcast(jnp.int32)  # i32(8, 128)
      )
      y_ref[...] = ref[...]

    x = jnp.arange(32 * 256, dtype=jnp.int32).reshape((32, 256))
    y = self.pallas_call(
        kernel,
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.int32),
    )(x)
    np.testing.assert_array_equal(y, x[8:16, :128])


@jtu.thread_unsafe_test_class()  # debug print test is not thread safe
class PallasCallPrintTest(PallasBaseTest):

  def test_debug_print(self):
    @functools.partial(
        self.pallas_call,
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )
    def kernel(x_ref, o_ref):
      pl.debug_print('It works!')

    x = jnp.arange(8 * 128, dtype=jnp.float32).reshape((8, 128))
    compiled_kernel = (
        jax.jit(kernel)
        .lower(x)
        .compile({'xla_tpu_enable_log_recorder': 'true'})
    )
    with jtu.capture_stderr() as get_output:
      jax.block_until_ready(compiled_kernel(x))
    self.assertIn('It works!', get_output())

  def test_debug_print_with_values(self):
    @functools.partial(
        self.pallas_call,
        in_specs=(pl.BlockSpec(memory_space=pltpu.SMEM),),
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )
    def kernel(x_ref, o_ref):
      pl.debug_print('x[0] == {}', x_ref[0])

    x = jnp.array([42, 24]).astype(jnp.int32)
    compiled_kernel = (
        jax.jit(kernel)
        .lower(x)
        .compile({'xla_tpu_enable_log_recorder': 'true'})
    )
    with jtu.capture_stderr() as get_output:
      jax.block_until_ready(compiled_kernel(x))
    self.assertIn('x[0] == 42', get_output())

  @parameterized.named_parameters(
      (f"{'_'.join(map(str, shape))}_{dtype.__name__}", shape, dtype)
      for shape in (
          (2, 8, 128),
          # test unaligned shapes
          (3,),
          (3, 4),
          (2, 3, 4),
          (2, 9, 129),
      )
      for dtype in (jnp.int32, jnp.uint32, jnp.float32)
  )
  def test_debug_print_vector(self, shape, dtype):
    @functools.partial(
        self.pallas_call,
        out_shape=jax.ShapeDtypeStruct(shape, dtype),
    )
    def kernel(x_ref, o_ref):
      pl.debug_print("{}", x_ref[...])
      o_ref[...] = x_ref[...]

    n = np.prod(shape)
    x = jnp.arange(n, dtype=dtype).reshape(shape)
    compiled_kernel = (
        jax.jit(kernel)
        .lower(x)
        .compile({"xla_tpu_enable_log_recorder": "true"})
    )
    with jtu.capture_stderr() as get_output:
      jax.block_until_ready(compiled_kernel(x))
    output = get_output()
    numbers = [
        int(num)
        for line in output.splitlines()
        if (match := re.search(r"\{(.*)", line))  # extract contents after `{`
        for num in re.findall(r"\d+", match.group(1))
    ]
    # Check if the numbers in the output match the values generated by `arange`.
    self.assertLen(numbers, n)
    self.assertTrue(all(num == i for i, num in enumerate(numbers)))


class PallasCallTraceTest(PallasBaseTest):

  @jtu.thread_unsafe_test()  # stdout redirection is not thread safe
  def test_trace_start_stop_match(self):
    def kernel(o_ref):
      with jax.named_scope('scope1'):
        o_ref[...] = jnp.zeros_like(o_ref[...])

    with string_stdout() as msg:
      _ = self.pallas_call(
        kernel,
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
        debug=True,
      )()
      # TODO(justinfu): Add an official lowering API to get the MLIR.
      debug_string = msg.getvalue()

    num_start = debug_string.count('tpu.trace_start')
    num_stop = debug_string.count('tpu.trace_stop')
    self.assertEqual(num_start, 1)
    self.assertEqual(num_stop, 1)

  @jtu.thread_unsafe_test()  # stdout redirection is not thread safe
  def test_run_scoped(self):
    def kernel(o_ref):
      def scope1():
        with jax.named_scope('scope1'):
          o_ref[...] = jnp.zeros_like(o_ref[...])
      pl.run_scoped(scope1)

      def scope2():
        with jax.named_scope('scope2'):
          o_ref[...] = o_ref[...] + 1
      pl.run_scoped(scope2)

    with string_stdout() as msg:
      _ = self.pallas_call(
        kernel,
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
        debug=True,
      )()
      # TODO(justinfu): Add an official lowering API to get the MLIR.
      debug_string = msg.getvalue()

    num_start = debug_string.count('tpu.trace_start')
    num_stop = debug_string.count('tpu.trace_stop')
    self.assertEqual(num_start, 2)
    self.assertEqual(num_stop, 2)


class PallasCallTPUBooleanTest(PallasBaseTest):
  """Tests for loading/storing from bool memrefs on TPUs.

  We specifically test bools because they have special handling.
  Bools are stored as integers inside of memrefs, and we typecast to/from
  bools automatically on load.
  """

  INTERPRET: bool = False

  @parameterized.parameters((False,), (True,))
  def test_scalar_bool_load_store(self, value):
    def kernel(x_ref, o_ref):
      o_ref[0, 0] = jnp.logical_not(x_ref[0, 0])
    input = jnp.array([[value]])
    output_shape = jax.ShapeDtypeStruct((1, 1), jnp.bool_)
    result = self.pallas_call(
        kernel,
        in_specs=[pl.BlockSpec(memory_space=pltpu.SMEM)],
        out_specs=pl.BlockSpec(memory_space=pltpu.SMEM),
        out_shape=output_shape,
    )(input)
    np.testing.assert_array_equal(result, jnp.logical_not(input))

  @parameterized.parameters((False,), (True,))
  def test_scalar_bool_run_scoped(self, value):
    if self.INTERPRET:
      self.skipTest('run_scoped not supported in non-interpret mode.')
    def kernel(x_ref, o_ref):
      def inner_scope(scoped_ref):
        scoped_ref[0, 0] = jnp.logical_not(x_ref[0, 0])
        o_ref[0, 0] = scoped_ref[0, 0]
      pl.run_scoped(inner_scope, pltpu.SMEM((1, 1), dtype=jnp.bool_))
    input_arr = jnp.array([[value]])
    output_shape = jax.ShapeDtypeStruct((1, 1), jnp.bool_)
    result = self.pallas_call(
        kernel,
        in_specs=[pl.BlockSpec(memory_space=pltpu.SMEM)],
        out_specs=pl.BlockSpec(memory_space=pltpu.SMEM),
        out_shape=output_shape,
    )(input_arr)
    np.testing.assert_array_equal(result, jnp.logical_not(input_arr))

  def test_vector_bool_load_store(self):
    def kernel(x_ref, o_ref):
      o_ref[...] = x_ref[...]
    input = jax.random.bernoulli(jax.random.key(0), p=0.5, shape=(8, 128))
    output_shape = jax.ShapeDtypeStruct((8, 128), jnp.bool_)
    result = self.pallas_call(
        kernel,
        in_specs=[pl.BlockSpec(memory_space=pltpu.VMEM)],
        out_specs=pl.BlockSpec(memory_space=pltpu.VMEM),
        out_shape=output_shape,
    )(input)
    np.testing.assert_array_equal(result, input)

  def test_vector_bool_masking_with_indexing(self):
    def kernel(mask_ref, true_ref, false_ref, o_ref):
      o_ref[0, ...] = jnp.where(
          mask_ref[0, ...], true_ref[0, ...], false_ref[0, ...])
    key = jax.random.key(0)
    k1, k2, k3 = jax.random.split(key, 3)
    values_1 = jax.random.normal(k1, (1, 256, 256), jnp.float32)
    values_2 = jax.random.normal(k2, (1, 256, 256), jnp.float32)
    mask = jax.random.bernoulli(k3, p=0.5, shape=(1, 256, 256))
    output_shape = jax.ShapeDtypeStruct((1, 256, 256), jnp.float32)
    result = self.pallas_call(
        kernel,
        in_specs=[pl.BlockSpec(memory_space=pltpu.VMEM),
                  pl.BlockSpec(memory_space=pltpu.VMEM),
                  pl.BlockSpec(memory_space=pltpu.VMEM),
                  ],
        out_specs=pl.BlockSpec(memory_space=pltpu.VMEM),
        out_shape=output_shape,
    )(mask, values_1, values_2)
    expected = jnp.where(mask, values_1, values_2)
    np.testing.assert_array_equal(result, expected)

  def test_bool_dma_not_implemented(self):
    if not jtu.is_device_tpu_at_least(4):
      self.skipTest('DMAs not supported on TPU generations <= 3')
    if self.INTERPRET:
      self.skipTest('Test only applies to non-interpret mode.')
    num_devices = jax.local_device_count()
    def kernel(x_ref, o_ref, send_sem, recv_sem):
      index = lax.axis_index('x')
      neighbor = lax.rem(index + 1, num_devices)
      copy = pltpu.make_async_remote_copy(x_ref,
                                   o_ref,
                                   send_sem,
                                   recv_sem,
                                   device_id=(0, neighbor))
      copy.start()
      copy.wait()
    input_arr = jnp.ones((8, 128), dtype=jnp.bool_)
    output_shape = jax.ShapeDtypeStruct((8, 128), jnp.bool_)
    grid_spec = pltpu.PrefetchScalarGridSpec(
      num_scalar_prefetch=0,
      in_specs=[pl.BlockSpec(memory_space=pltpu.VMEM)],
      out_specs=pl.BlockSpec(memory_space=pltpu.VMEM),
      grid=(1,),
      scratch_shapes=[pltpu.SemaphoreType.DMA] * 2,
    )
    test_fn = self.pallas_call(
          kernel,
          grid_spec=grid_spec,
          out_shape=output_shape,
      )
    with self.assertRaisesRegex(
        Exception, 'DMAs with bool dtypes are not supported.'):
      devices = mesh_utils.create_device_mesh((num_devices,))
      mesh = jax.sharding.Mesh(devices, ('x',))
      sharding = jax.sharding.NamedSharding(mesh, P(None, 'x'))
      input_arr = jax.device_put(input_arr, sharding)
      jax.jit(
            shard_map.shard_map(
                test_fn,
                mesh=mesh,
                in_specs=P(None, 'x'),
                out_specs=P(None, 'x'),
                check_vma=False
            )
      )(input_arr)


class PallasCallTPUBooleanInterpretTest(PallasCallTPUBooleanTest):
  INTERPRET: bool = True


class PallasCallTPUCheckifyTest(PallasBaseTest):
  @parameterized.parameters((2,), (5,), (6,), (7,))
  def test_checkify_with_scalar_prefetch(self, threshold):
    def body(scalar_ref, x_ref, o_ref):
      scalar = scalar_ref[pl.program_id(0)]
      o_ref[...] = x_ref[...]
      checkify.check(scalar < threshold, 'failed on value {x}', x=scalar)

    s = jnp.array([4, 3, 2, 6, 3, 5, 2, 7], jnp.int32)
    x = jnp.arange(8 * 8 * 128, dtype=jnp.int32).reshape((8 * 8, 128))

    def _x_transform(i, s_ref):
      return (s_ref[i], 0)

    pallas_call = self.pallas_call(
        body,
        out_shape=jax.ShapeDtypeStruct(x.shape, jnp.int32),
        grid_spec=pltpu.PrefetchScalarGridSpec(
            num_scalar_prefetch=1,
            in_specs=[
                pl.BlockSpec((x.shape[0] // 8, x.shape[1]), _x_transform),
            ],
            out_specs=pl.BlockSpec(
                (x.shape[0] // 8, x.shape[1]), lambda i, _: (i, 0)
            ),
            grid=8,
        ),
    )
    checked_call = checkify.checkify(pallas_call)
    err, out = checked_call(s, x)
    expected_error_value = s[jnp.argmax(s >= threshold)]
    with self.assertRaisesRegex(
        checkify.JaxRuntimeError, f'failed on value {expected_error_value}'):
      err.throw()
    np.testing.assert_allclose(out, x.reshape((8, 8, -1))[s].reshape(x.shape))

  def test_checkify_with_scratch(self):
    def body(x_ref, o_ref, scratch_ref):
      scratch_ref[...] = x_ref[...]
      o_ref[...] = scratch_ref[...]
      all_nequal = ~jnp.all(o_ref[...] == x_ref[...])
      checkify.check(all_nequal, 'x_ref equals o_ref id=({x}, {y})',
                     x=pl.program_id(0), y=pl.program_id(1))

    x = jax.random.uniform(jax.random.key(0), (128, 512), dtype=jnp.float32)
    pallas_call = self.pallas_call(
        body,
        out_shape=jax.ShapeDtypeStruct(x.shape, jnp.float32),
        grid_spec=pltpu.PrefetchScalarGridSpec(
            num_scalar_prefetch=0,
            in_specs=[
                pl.BlockSpec((32, 128), lambda i, j: (i, j)),
            ],
            out_specs=pl.BlockSpec((32, 128), lambda i, j: (i, j)),
            scratch_shapes=[pltpu.VMEM((32, 128), dtype=jnp.float32)],
            grid=(4, 4),
        ),
    )
    checked_call = checkify.checkify(pallas_call)
    err, out = checked_call(x)
    with self.assertRaisesRegex(
        checkify.JaxRuntimeError, r'x_ref equals o_ref id=\(0, 0\)'):
      err.throw()
    np.testing.assert_allclose(out, x)

  @parameterized.parameters((4,), (9,))
  def test_checkify_with_dynamic_grid(self, iteration):
    grid_size = 4
    shape = (8, 128)
    result_ty = jax.ShapeDtypeStruct(shape, jnp.float32)

    def kernel(y_ref):
      @pl.when(pl.program_id(0) == 0)
      def _init():
        y_ref[...] = jnp.zeros_like(y_ref)
      y_ref[...] += 1
      @pl.when(pl.program_id(0) == iteration)
      def _():
        checkify.check(False, f"error on iteration {iteration}")

    @jax.jit
    def dynamic_kernel(steps):
      pallas_call = self.pallas_call(
          kernel,
          grid=(steps * 2,),
          out_specs=pl.BlockSpec(shape, lambda i: (0, 0)),
          out_shape=result_ty,
      )
      return checkify.checkify(pallas_call)()

    err, result = dynamic_kernel(jnp.int32(grid_size))
    if iteration < grid_size * 2:
      with self.assertRaisesRegex(
          checkify.JaxRuntimeError, f"error on iteration {iteration}"):
        err.throw()
    np.testing.assert_array_equal(
        result, np.full(shape, grid_size * 2.0, np.float32)
    )


class PallasCallTPUCheckifyInterpretTest(PallasCallTPUCheckifyTest):
  INTERPRET: bool = True


class PrettyPrintingTest(PallasBaseTest):

  @parameterized.parameters(
      (
          lambda i: (i, pl.ds(0, 8), pl.ds(0, 128)),
          'dma_start(p0) c[d,:,:] -> e[...] f',
      ),
      (
          lambda i: (0, pl.ds(i, 8), pl.ds(0, 128)),
          'dma_start(p0) c[0,d:d+8,:] -> e[...] f',
      ),
      (
          lambda i: (i, pl.ds(2, 4), pl.ds(0, 100)),
          'dma_start(p0) c[d,2:6,:100] -> e[...] f',
      ),
      (
          lambda i: (i, pl.ds(2, 6), pl.ds(4, 100)),
          'dma_start(p0) c[d,2:,4:104] -> e[...] f',
      ),
  )
  def test_dma_custom_pretty_print(self, indexer, expected):
    def body(x_hbm_ref, i):
      def inner(x_ref, sem):
        pltpu.async_copy(x_hbm_ref.at[indexer(i)], x_ref, sem).wait()

      pl.run_scoped(
          inner, pltpu.VMEM((8, 128), jnp.float32), pltpu.SemaphoreType.DMA
      )
      return []

    jaxpr, _, _ = pe.trace_to_jaxpr_dynamic(
        wrap_init(body, 2),
        [
            state.shaped_array_ref((2, 8, 128), jnp.int32),
            jax.core.ShapedArray((), jnp.int32),
        ],
    )
    self.assertIn(expected, jaxpr.pretty_print(use_color=False))


class MiscellaneousTest(PallasBaseTest):
  """Tests for reported bugs. Only pass in interpret mode unless fixed."""

  def test_float32_stack(self):
    x = np.arange(128, dtype=jnp.float32).reshape(1, 128)
    y = x + 128

    def kernel(x_ref, y_ref, out_ref):
      out_ref[...] = jnp.stack([x_ref[...], y_ref[...]], axis=1)

    out = self.pallas_call(
        kernel, out_shape=jax.ShapeDtypeStruct((1, 2, 128), jnp.float32)
    )(x, y)
    np.testing.assert_array_equal(out, np.stack([x, y], axis=1))

  def test_lane_to_chunk_reshape_bf16(self):
    if not jtu.if_cloud_tpu_at_least(2025, 7, 12):
      self.skipTest('Needs a newer libTPU')
    if not jtu.is_device_tpu_at_least(4):
      self.skipTest('Operation not supported on this TPU version.')
    x = np.arange(256 * 1024, dtype=jnp.bfloat16).reshape(1, 256, 1024)

    def kernel(x_ref, out_ref):
      out_ref[...] = jnp.reshape(x_ref[...], (1, 256, 8, 128))

    out = self.pallas_call(
        kernel, out_shape=jax.ShapeDtypeStruct((1, 256, 8, 128), jnp.bfloat16)
    )(x)
    np.testing.assert_array_equal(out, np.reshape(x, (1, 256, 8, 128)))

  def test_lane_to_chunk_broadcast_fp32(self):
    x = np.arange(256 * 128, dtype=jnp.float32).reshape(1, 256, 128)

    def kernel(x_ref, out_ref):
      out_ref[...] = jnp.broadcast_to(
          jnp.expand_dims(x_ref[...], 2), (1, 256, 8, 128)
      )

    out = self.pallas_call(
        kernel, out_shape=jax.ShapeDtypeStruct((1, 256, 8, 128), jnp.float32)
    )(x)
    np.testing.assert_array_equal(
        out, np.broadcast_to(np.expand_dims(x, 2), (1, 256, 8, 128))
    )

  @only_passes_in_interpret()
  def test_lane_dynamic_slice(self):
    """b/346849973"""
    x = np.arange(128, dtype=jnp.float32)

    def kernel(x_ref, out_ref):
      out_ref[...] = lax.dynamic_slice_in_dim(x_ref[...], 64, 1, 0)

    out = self.pallas_call(
        kernel, out_shape=jax.ShapeDtypeStruct((1,), jnp.float32)
    )(x)
    np.testing.assert_array_equal(out, x[64:65])

  def test_lane_broadcast_bf16(self):
    x = np.arange(256, dtype=jnp.bfloat16).reshape(256, 1)

    def kernel(x_ref, out_ref):
      out_ref[...] = jnp.broadcast_to(x_ref[...], (256, 512))

    out = self.pallas_call(
        kernel, out_shape=jax.ShapeDtypeStruct((256, 512), jnp.bfloat16)
    )(x)
    np.testing.assert_array_equal(out, np.broadcast_to(x, (256, 512)))

  def test_bfloat16_to_uint32_bitcast(self):
    x = np.arange(16 * 2 * 256, dtype=jnp.bfloat16).reshape(16, 2, 256)

    def kernel(x_ref, out_ref):
      out_ref[...] = pltpu.bitcast(x_ref[...], jnp.uint32)

    out = self.pallas_call(
        kernel, out_shape=jax.ShapeDtypeStruct((16, 1, 256), jnp.uint32)
    )(x)
    np.testing.assert_array_equal(out, state_utils.bitcast(x, jnp.uint32))

  def test_roll_partial_with_static_shift(self):
    if not jtu.if_cloud_tpu_at_least(2025, 5, 15):
      self.skipTest('Needs a newer libtpu')
    x = np.arange(8192, dtype=jnp.float32).reshape(128, 64)

    def kernel(x_ref, out_ref):
      out_ref[...] = pltpu.roll(x_ref[...], 3, 1)

    out = self.pallas_call(
        kernel, out_shape=jax.ShapeDtypeStruct((128, 64), jnp.float32)
    )(x)
    np.testing.assert_array_equal(out, np.roll(x, 3, 1))

  def test_roll_partial_with_dynamic_shift(self):
    if not jtu.if_cloud_tpu_at_least(2025, 5, 15):
      self.skipTest('Needs a newer libtpu')
    if self.INTERPRET:
      self.skipTest('Test only applies to non-interpret mode.')
    x = np.arange(8192, dtype=jnp.float32).reshape(128, 64)

    def kernel(x_ref, out_ref):
      amount = x_ref[0, 0].astype(jnp.int32)
      out_ref[...] = pltpu.roll(x_ref[...], amount, 1)

    with self.assertRaisesRegex(Exception, 'unsupported unaligned shape'):
      _ = self.pallas_call(
          kernel, out_shape=jax.ShapeDtypeStruct((128, 64), jnp.float32)
      )(x)

  def test_retiling1(self):
    if not jtu.if_cloud_tpu_at_least(2025, 7, 2):
      self.skipTest('Needs a newer libtpu')
    x = np.arange(1024, dtype=jnp.bfloat16).reshape(1024)

    def kernel(x_ref, out_ref):
      out_ref[:, :] = jnp.reshape(x_ref[:].astype(jnp.float32), (8, 128))

    out = self.pallas_call(
        kernel,
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )(x)

    np.testing.assert_array_equal(out, np.reshape(x, (8, 128)))

  def test_retiling2(self):
    x = np.arange(1 * 8 * 1024, dtype=jnp.bfloat16).reshape(1, 8, 1024)

    def kernel(x_ref, out_ref):
      out_ref[:, :, :] = jnp.reshape(
          x_ref[:, 7, :].astype(jnp.float32), (1, 8, 128)
      )

    out = self.pallas_call(
        kernel,
        out_shape=jax.ShapeDtypeStruct((1, 8, 128), jnp.float32),
    )(x)

    np.testing.assert_array_equal(out, np.reshape(x[:, 7, :], (1, 8, 128)))

  def test_sublane_adding_shape_cast_f32(self):
    if not jtu.if_cloud_tpu_at_least(2025, 7, 12):
      self.skipTest('Needs a newer libTPU')
    x = np.arange(8 * 128, dtype=jnp.float32).reshape(8, 128)

    def kernel(x_ref, out_ref):
      out_ref[:, 0, :] = x_ref[:, :]

    out = self.pallas_call(
        kernel, out_shape=jax.ShapeDtypeStruct((8, 1, 128), jnp.float32)
    )(x)

    np.testing.assert_array_equal(out, np.reshape(x, (8, 1, 128)))

  def test_sublane_adding_shape_cast_bf16(self):
    if not jtu.if_cloud_tpu_at_least(2025, 7, 12):
      self.skipTest('Needs a newer libTPU')
    if not jtu.is_device_tpu_at_least(4):
      self.skipTest('Operation not supported on this TPU version.')
    x = np.arange(8 * 128, dtype=jnp.bfloat16).reshape(8, 128)

    def kernel(x_ref, out_ref):
      out_ref[:, 0, :] = x_ref[:, :]

    out = self.pallas_call(
        kernel, out_shape=jax.ShapeDtypeStruct((8, 1, 128), jnp.bfloat16)
    )(x)

    np.testing.assert_array_equal(out, np.reshape(x, (8, 1, 128)))

  def test_mixed_strides(self):
    x = np.full((8, 128), 1.0, dtype=jnp.float32)
    y = np.full((8, 2, 128), 2.0, dtype=jnp.bfloat16)

    def kernel(x_ref, y_ref, out_ref):
      out_ref[:, :] = x_ref[:, :] + y_ref[:, 1, :].astype(jnp.float32)

    out = self.pallas_call(
        kernel,
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )(x, y)

    np.testing.assert_array_equal(
        out, np.full((8, 128), 3.0, dtype=jnp.float32)
    )

  def test_sum(self):
    x = np.zeros((8, 2, 8, 128), dtype=jnp.float32)

    def kernel(x_ref, out_ref):
      out_ref[:, :, :] = jnp.sum(x_ref[:, :, :, :], 2)

    out = self.pallas_call(
        kernel, out_shape=jax.ShapeDtypeStruct((8, 2, 128), jnp.float32)
    )(x)

    np.testing.assert_array_equal(out, np.zeros((8, 2, 128), dtype=jnp.float32))

  @only_passes_in_interpret()
  def test_transpose(self):
    """b/356475128"""
    x = np.zeros((8, 2, 8, 128), dtype=jnp.float32)

    def kernel(x_ref, out_ref):
      out_ref[:, :, :, :] = jnp.transpose(x_ref[:, :, :, :], (0, 2, 1, 3))

    out = self.pallas_call(
        kernel, out_shape=jax.ShapeDtypeStruct((8, 8, 2, 128), jnp.float32)
    )(x)

    np.testing.assert_array_equal(
        out, np.zeros((8, 8, 2, 128), dtype=jnp.float32)
    )

  # (q, m, n) -> (q, m * n) where n % 128 == 0
  @parameterized.parameters(
      (q, m, n, dtype)
      for (q, m, n), dtype in itertools.product(
          [
              (32, 16, 512),
              (20, 19, 512),
              (5, 3, 256),
              (9, 15, 256),
              (3, 2, 256),
          ],
          [jnp.float32, jnp.uint32, jnp.bfloat16, jnp.int8],
      )
  )
  def test_reshape_two_minor_dims_to_R2(self, q, m, n, dtype):
    if not jtu.if_cloud_tpu_at_least(2025, 7, 12):
      self.skipTest('Needs a newer libTPU')
    if (dtype == jnp.bfloat16 and not jtu.is_device_tpu_at_least(4)) or (
        dtype == jnp.int8 and not jtu.is_device_tpu_at_least(5)
    ):
      self.skipTest('Operation not supported on this TPU version.')
    def kernel(x_ref, y_ref):
      y_ref[...] = x_ref[...].reshape(
          x_ref.shape[0], x_ref.shape[1] * x_ref.shape[2]
      )

    x = np.arange(q * m * n, dtype=dtype).reshape(q, m, n)
    out = self.pallas_call(
        kernel,
        out_shape=jax.ShapeDtypeStruct((q, m * n), dtype),
    )(x)
    jax.numpy.set_printoptions(threshold=jax.numpy.inf)
    expected = x.reshape([q, m * n])
    np.testing.assert_array_equal(out, x.reshape([q, m * n]))

  # (q, m, n, k) -> (q, m, n * k) where k % 128 == 0
  @parameterized.parameters(
      (q, m, n, k, dtype)
      for (q, m, n, k), dtype in itertools.product(
          [
              (3, 8, 17, 512),
              (1, 8, 9, 256),
              (1, 8, 3, 256),
              (10, 1, 4, 256),
              (1, 2, 2, 256),
              (1, 9, 3, 256),
          ],
          [jnp.float32, jnp.uint32, jnp.bfloat16, jnp.int8],
      )
  )
  def test_reshape_two_minor_dims_to_R3(self, q, m, n, k, dtype):
    if not jtu.if_cloud_tpu_at_least(2025, 7, 12):
      self.skipTest('Needs a newer libTPU')
    if (dtype == jnp.bfloat16 and not jtu.is_device_tpu_at_least(4)) or (
        dtype == jnp.int8 and not jtu.is_device_tpu_at_least(5)
    ):
      self.skipTest('Operation not supported on this TPU version.')
    def kernel(x_ref, y_ref):
      y_ref[...] = x_ref[...].reshape(
          x_ref.shape[0], x_ref.shape[1], x_ref.shape[2] * x_ref.shape[3]
      )

    x = np.arange(q * m * n * k, dtype=dtype).reshape(q, m, n, k)
    out = self.pallas_call(
        kernel,
        out_shape=jax.ShapeDtypeStruct((q, m, n * k), dtype),
    )(x)
    np.testing.assert_array_equal(out, x.reshape([q, m, n * k]))

  # (p, q, m, n, k) -> (p, q * m * n * k) where k % 128 == 0
  @parameterized.parameters(
      (p, q, m, n, k, dtype)
      for (p, q, m, n, k), dtype in itertools.product(
          [
              (5, 3, 8, 17, 512),
              (6, 1, 8, 9, 256),
              (16, 1, 8, 3, 256),
              (3, 2, 1, 4, 256),
              (1, 7, 2, 2, 256),
          ],
          [jnp.float32, jnp.uint32, jnp.bfloat16, jnp.int8],
      )
  )
  def test_reshape_four_minor_dims_to_R2(self, p, q, m, n, k, dtype):
    if not jtu.if_cloud_tpu_at_least(2025, 7, 12):
      self.skipTest('Needs a newer libTPU')
    if (dtype == jnp.bfloat16 and not jtu.is_device_tpu_at_least(4)) or (
        dtype == jnp.int8 and not jtu.is_device_tpu_at_least(5)
    ):
      self.skipTest('Operation not supported on this TPU version.')
    def kernel(x_ref, y_ref):
      y_ref[...] = x_ref[...].reshape(
          x_ref.shape[0],
          x_ref.shape[1] * x_ref.shape[2] * x_ref.shape[3] * x_ref.shape[4],
      )

    x = np.arange(p * q * m * n * k, dtype=dtype).reshape(p, q, m, n, k)
    out = self.pallas_call(
        kernel,
        out_shape=jax.ShapeDtypeStruct((p, q * m * n * k), dtype),
    )(x)
    np.testing.assert_array_equal(out, x.reshape([p, q * m * n * k]))

  # (q, m, n, k) -> (q, m, 1, n * k) where k % 128 == 0
  @parameterized.parameters(
      (q, m, n, k, dtype)
      for (q, m, n, k), dtype in itertools.product(
          [
              (10, 1, 4, 256),
          ],
          [jnp.float32, jnp.uint32, jnp.bfloat16, jnp.int8],
      )
  )
  def test_reshape_two_minor_dims_preserve_rank(self, q, m, n, k, dtype):
    if not jtu.if_cloud_tpu_at_least(2025, 7, 12):
      self.skipTest('Needs a newer libTPU')
    if (dtype == jnp.bfloat16 and not jtu.is_device_tpu_at_least(4)) or (
        dtype == jnp.int8 and not jtu.is_device_tpu_at_least(5)
    ):
      self.skipTest('Operation not supported on this TPU version.')
    def kernel(x_ref, y_ref):
      y_ref[...] = (
          x_ref[...]
          .reshape(
              x_ref.shape[0], x_ref.shape[1], x_ref.shape[2] * x_ref.shape[3]
          )
          .reshape(
              x_ref.shape[0], 1, x_ref.shape[1], x_ref.shape[2] * x_ref.shape[3]
          )
      )

    q, m, n, k = 10, 1, 4, 256
    x = np.arange(q * m * n * k, dtype=dtype).reshape(q, m, n, k)
    out = self.pallas_call(
        kernel,
        out_shape=jax.ShapeDtypeStruct((q, m, 1, n * k), dtype),
    )(x)
    np.testing.assert_array_equal(out, x.reshape([q, m, 1, n * k]))

  # (q, m, n, k) -> (q * m, n * k) where k % 128 == 0
  @parameterized.parameters(
      (q, m, n, k, dtype)
      for (q, m, n, k), dtype in itertools.product(
          [
              (3, 9, 17, 512),
              (1, 8, 9, 256),
              (1, 8, 3, 384),
              (10, 1, 4, 256),
              (1, 2, 2, 256),
          ],
          [jnp.float32, jnp.uint32, jnp.bfloat16, jnp.int8],
      )
  )
  def test_reshape_fold_two_leading_dims_and_two_minor_dims_R4_to_R2(
      self, q, m, n, k, dtype
  ):
    if not jtu.if_cloud_tpu_at_least(2025, 7, 12):
      self.skipTest('Needs a newer libTPU')
    if (dtype == jnp.bfloat16 and not jtu.is_device_tpu_at_least(4)) or (
        dtype == jnp.int8 and not jtu.is_device_tpu_at_least(5)
    ):
      self.skipTest('Operation not supported on this TPU version.')
    def kernel(x_ref, y_ref):
      y_ref[...] = x_ref[...].reshape(
          x_ref.shape[0] * x_ref.shape[1], x_ref.shape[2] * x_ref.shape[3]
      )

    x = np.arange(q * m * n * k, dtype=dtype).reshape(q, m, n, k)
    out = self.pallas_call(
        kernel,
        out_shape=jax.ShapeDtypeStruct((q * m, n * k), dtype),
    )(x)
    np.testing.assert_array_equal(out, x.reshape([q * m, n * k]))

  # (q * m, n, k) -> (q, m, n * k) where k % 128 == 0
  @parameterized.parameters(
      (q, m, n, k, dtype)
      for (q, m, n, k), dtype in itertools.product(
          [
              (2, 2, 17, 512),
              (3, 2, 3, 256),
              (1, 5, 4, 384),
          ],
          [jnp.float32, jnp.uint32, jnp.bfloat16, jnp.int8],
      )
  )
  def test_reshape_unfold_leading_dim_and_fold_two_minor_dims_R3_to_R3(
      self, q, m, n, k, dtype
  ):
    if not jtu.if_cloud_tpu_at_least(2025, 7, 12):
      self.skipTest('Needs a newer libTPU')
    if (dtype == jnp.bfloat16 and not jtu.is_device_tpu_at_least(4)) or (
        dtype == jnp.int8 and not jtu.is_device_tpu_at_least(5)
    ):
      self.skipTest('Operation not supported on this TPU version.')
    def kernel(x_ref, y_ref):
      y_ref[...] = x_ref[...].reshape(
          q,
          m,
          x_ref.shape[1] * x_ref.shape[2],
      )

    x = np.arange(q * m * n * k, dtype=dtype).reshape(q * m, n, k)
    out = self.pallas_call(
        kernel,
        out_shape=jax.ShapeDtypeStruct((q, m, n * k), dtype),
    )(x)
    np.testing.assert_array_equal(out, x.reshape([q, m, n * k]))

  # (q * m, n * k) -> (q, m, n, k) where k % 128 == 0
  @parameterized.parameters(
      (q, m, n, k, dtype)
      for (q, m, n, k), dtype in itertools.product(
          [
              (2, 2, 17, 512),
              (3, 2, 3, 256),
              (1, 5, 4, 384),
          ],
          [jnp.float32, jnp.uint32, jnp.bfloat16, jnp.int8],
      )
  )
  def test_reshape_unfold_leading_and_minor_dims_R2_to_R4(
      self, q, m, n, k, dtype
  ):
    if not jtu.if_cloud_tpu_at_least(2025, 7, 12):
      self.skipTest('Needs a newer libTPU')
    if (dtype == jnp.bfloat16 and not jtu.is_device_tpu_at_least(4)) or (
        dtype == jnp.int8 and not jtu.is_device_tpu_at_least(5)
    ):
      self.skipTest('Operation not supported on this TPU version.')
    def kernel(x_ref, y_ref):
      y_ref[...] = x_ref[...].reshape(q, m, n, k)

    x = np.arange(q * m * n * k, dtype=dtype).reshape(q * m, n * k)
    out = self.pallas_call(
        kernel,
        out_shape=jax.ShapeDtypeStruct((q, m, n, k), dtype),
    )(x)
    np.testing.assert_array_equal(out, x.reshape([q, m, n, k]))

  # (q, m, n * k) -> (q * m, n, k) where k % 128 == 0
  @parameterized.parameters(
      (q, m, n, k, dtype)
      for (q, m, n, k), dtype in itertools.product(
          [
              (2, 2, 17, 512),
              (3, 2, 8, 256),
              (1, 5, 4, 384),
          ],
          [jnp.float32, jnp.uint32, jnp.bfloat16, jnp.int8],
      )
  )
  def test_reshape_fold_leading_dims_and_unfold_minor_dim(
      self, q, m, n, k, dtype
  ):
    if not jtu.if_cloud_tpu_at_least(2025, 7, 12):
      self.skipTest('Needs a newer libTPU')
    if (dtype == jnp.bfloat16 and not jtu.is_device_tpu_at_least(4)) or (
        dtype == jnp.int8 and not jtu.is_device_tpu_at_least(5)
    ):
      self.skipTest('Operation not supported on this TPU version.')
    def kernel(x_ref, y_ref):
      y_ref[...] = x_ref[...].reshape(q * m, n, k)

    x = np.arange(q * m * n * k, dtype=dtype).reshape(q, m, n * k)
    out = self.pallas_call(
        kernel,
        out_shape=jax.ShapeDtypeStruct((q * m, n, k), dtype),
    )(x)
    np.testing.assert_array_equal(out, x.reshape([q * m, n, k]))

  # (q, m, n, k) -> (q, m * n, k) where k % 128 == 0
  @parameterized.parameters(
      (q, m, n, k, dtype)
      for (q, m, n, k), dtype in itertools.product(
          [
              (2, 2, 17, 512),
              (3, 2, 8, 256),
              (1, 5, 4, 384),
          ],
          [jnp.float32, jnp.uint32, jnp.bfloat16, jnp.int8],
      )
  )
  def test_reshape_fold_middle_dims(self, q, m, n, k, dtype):
    if not jtu.if_cloud_tpu_at_least(2025, 7, 12):
      self.skipTest('Needs a newer libTPU')
    if (dtype == jnp.bfloat16 and not jtu.is_device_tpu_at_least(4)) or (
        dtype == jnp.int8 and not jtu.is_device_tpu_at_least(5)
    ):
      self.skipTest('Operation not supported on this TPU version.')
    def kernel(x_ref, y_ref):
      y_ref[...] = x_ref[...].reshape(q, m * n, k)

    x = np.arange(q * m * n * k, dtype=dtype).reshape(q, m, n, k)
    out = self.pallas_call(
        kernel,
        out_shape=jax.ShapeDtypeStruct((q, m * n, k), dtype),
    )(x)
    np.testing.assert_array_equal(out, x.reshape([q, m * n, k]))

  # (q, m * n, k) -> (q, m, n, k) where k % 128 == 0
  @parameterized.parameters(
      (q, m, n, k, dtype)
      for (q, m, n, k), dtype in itertools.product(
          [
              (2, 2, 17, 512),
              (3, 2, 8, 256),
              (9, 5, 4, 384),
          ],
          [jnp.float32, jnp.uint32, jnp.bfloat16, jnp.int8],
      )
  )
  def test_reshape_unfold_middle_dims(self, q, m, n, k, dtype):
    if not jtu.if_cloud_tpu_at_least(2025, 7, 12):
      self.skipTest('Needs a newer libTPU')
    if (dtype == jnp.bfloat16 and not jtu.is_device_tpu_at_least(4)) or (
        dtype == jnp.int8 and not jtu.is_device_tpu_at_least(5)
    ):
      self.skipTest('Operation not supported on this TPU version.')
    def kernel(x_ref, y_ref):
      y_ref[...] = x_ref[...].reshape(q, m, n, k)

    x = np.arange(q * m * n * k, dtype=dtype).reshape(q, m * n, k)
    out = self.pallas_call(
        kernel,
        out_shape=jax.ShapeDtypeStruct((q, m, n, k), dtype),
    )(x)
    np.testing.assert_array_equal(out, x.reshape([q, m, n, k]))

  @parameterized.parameters([jnp.int8, jnp.bfloat16, jnp.float32])
  def test_reshape_shift_factor_from_minor_to_major(self, dtype):
    if not jtu.if_cloud_tpu_at_least(2025, 7, 12):
      self.skipTest('Needs a newer libTPU')
    if (dtype == jnp.bfloat16 and not jtu.is_device_tpu_at_least(4)) or (
        dtype == jnp.int8 and not jtu.is_device_tpu_at_least(5)
    ):
      self.skipTest('Operation not supported on this TPU version.')
    q0, m0, n0 = 1, 3, 7680
    q1, m1, n1 = 3, 10, 768
    def kernel(x_ref, y_ref):
      y_ref[...] = x_ref[...].reshape(q1, m1, n1)

    x = np.arange(q0 * m0 * n0, dtype=dtype).reshape(q0, m0, n0)
    out = self.pallas_call(
        kernel,
        out_shape=jax.ShapeDtypeStruct((q1, m1, n1), dtype),
    )(x)
    np.testing.assert_array_equal(out, x.reshape([q1, m1, n1]))


class MiscellaneousInterpretTest(MiscellaneousTest):
  INTERPRET: bool = True


class PallasKernelMetadataTest(PallasBaseTest):

  @parameterized.parameters(
      (dict(foo='bar'),),
      (dict(foo='afjafo'),),
      (dict(problem_info=json.dumps(dict(tiling_info=dict(bm=128, bk=128)))),),
  )
  def test_metadata_is_preserved(self, metadata):

    def kernel(x_ref, y_ref, out_ref):
      out_ref[...] = x_ref[...] + y_ref[...]

    x = jnp.arange(1024, dtype=jnp.float32).reshape((8, 128))
    y = jnp.arange(1024, dtype=jnp.float32).reshape((8, 128))

    @jax.jit
    def f(x, y):
      return self.pallas_call(
          kernel,
          out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
          metadata=metadata,
      )(x, y)

    hlo = f.lower(x, y).compile().as_text()
    self.assertIn(json.dumps(metadata), hlo)


if __name__ == '__main__':
  absltest.main(testLoader=jtu.JaxTestLoader())
