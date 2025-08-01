# Copyright 2024 The JAX Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""(Deviceless) tests for the Mosaic GPU MLIR dialect."""

from collections.abc import Callable

from absl.testing import parameterized
import jax
from jax import numpy as jnp
from jax._src import config
from jax._src import test_util as jtu
from jax._src.interpreters import mlir as mlir_interpreter
from jax._src.lib.mlir import ir
from jax._src.lib.mlir.dialects import arith
from jax._src.lib.mlir.dialects import func
from jax._src.lib.mlir.dialects import gpu
from jax._src.lib.mlir.dialects import llvm
from jax._src.lib.mlir.dialects import memref
from jax._src.lib.mlir.dialects import nvvm
from jax._src.lib.mlir.dialects import scf
from jax._src.lib.mlir.dialects import vector
from jax.experimental.mosaic import gpu as mgpu
from jax.experimental.mosaic.gpu import layouts
from jax.experimental.mosaic.gpu import utils as mgpu_utils
from jax.experimental.mosaic.gpu import dialect_lowering as lowering

_cext = mgpu.dialect._cext if mgpu.dialect is not None else None


config.parse_flags_with_absl()


def _make_ir_context():
  context = ir.Context()
  context.append_dialect_registry(mlir_interpreter.upstream_dialects)
  context.load_all_available_dialects()
  mgpu.dialect.register_dialect(context)
  return context


def walk_operations(op: ir.OpView, callback):
  for region in op.operation.regions:
    for block in region:
      for block_op in block:
        walk_operations(block_op, callback)
  callback(op)


def find_if(
    module: ir.Module, predicate: Callable[[ir.OpView], bool]
) -> list[ir.OpView]:
  result = []

  def callback(op: ir.OpView):
    if predicate(op):
      result.append(op)

  for op in module.body.operations:
    walk_operations(op, callback)
  return result


def is_mosaic_gpu_op(op: ir.OpView) -> bool:
  return op.name.startswith("mosaic_gpu.")


def workgroup_ptr_ty() -> ir.Type:
  workgroup_nvptx_address_space = mgpu_utils.gpu_address_space_to_nvptx(
      gpu.AddressSpace.Workgroup
  )
  return ir.Type.parse(f"!llvm.ptr<{workgroup_nvptx_address_space}>")


class MosaicGpuTest(parameterized.TestCase):

  def setUp(self):
    if jax.version._version != jax.lib.__version__:
      raise self.skipTest("Test requires matching jax and jaxlib versions")
    super().setUp()
    self.enter_context(_make_ir_context())
    self.enter_context(ir.Location.unknown())
    self.module = ir.Module.create()


class DialectTest(MosaicGpuTest):

  def test_dialect_module_is_loaded(self):
    self.assertTrue(_cext.globals._check_dialect_module_loaded("mosaic_gpu"))

  def test_initialize_barrier_op_result_memref_must_wrap_barriers(self):
    with ir.InsertionPoint(self.module.body):
      mgpu.dialect.initialize_barrier(
          ir.MemRefType.get((1, 2), ir.F32Type.get()),
          llvm.UndefOp(workgroup_ptr_ty()),
          arrival_count=1,
      )
    with self.assertRaisesRegex(
        ir.MLIRError, "must be memref of barrier values"
    ):
      self.module.operation.verify()

  def test_initialize_barrier_op_arrival_count_must_be_strictly_positive(self):
    with ir.InsertionPoint(self.module.body):
      mgpu.dialect.initialize_barrier(
          ir.MemRefType.get((1, 2), ir.Type.parse("!mosaic_gpu.barrier")),
          llvm.UndefOp(workgroup_ptr_ty()),
          arrival_count=0,
      )
    with self.assertRaisesRegex(ir.MLIRError, "value is positive"):
      self.module.operation.verify()

  def test_initialize_barrier_op_with_a_non_shared_base_pointer_fails(self):
    with ir.InsertionPoint(self.module.body):
      mgpu.dialect.initialize_barrier(
          ir.MemRefType.get((1, 2), ir.Type.parse("!mosaic_gpu.barrier")),
          llvm.UndefOp(ir.Type.parse(f"!llvm.ptr<{0}>")),
          arrival_count=1,
      )
    with self.assertRaisesRegex(ir.MLIRError, "pointer in address space 3"):
      self.module.operation.verify()

  def test_initialize_barrier_op_with_a_positive_arrival_count_passes(self):
    with ir.InsertionPoint(self.module.body):
      mgpu.dialect.initialize_barrier(
          ir.MemRefType.get((1, 2), ir.Type.parse("!mosaic_gpu.barrier")),
          llvm.UndefOp(workgroup_ptr_ty()),
          arrival_count=1,
      )
    self.assertTrue(self.module.operation.verify())
    self.assertIsInstance(
        self.module.body.operations[1], mgpu.dialect.InitializeBarrierOp
    )

  def test_async_load_op_dest_must_be_contiguous(self):
    with ir.InsertionPoint(self.module.body):
      func.FuncOp.from_py_func(
          ir.MemRefType.get([4, 8], ir.F32Type.get()),
          ir.MemRefType.get(
              [4, 8],
              ir.F32Type.get(),
              layout=ir.Attribute.parse("strided<[16, 1]>"),
          ),
          ir.MemRefType.get([], ir.Type.parse("!mosaic_gpu.barrier")),
          ir.IntegerType.get_signless(32),
          ir.IntegerType.get_signless(32),
          name="async_load",
      )(
          lambda source, destination, barrier, *indices: mgpu.dialect.async_load(
              source,
              destination,
              barrier,
              indices,
              slice_lengths=[4, 8],
              collective=ir.ArrayAttr.get([]),
          )
      )

    with self.assertRaisesRegex(
        ir.MLIRError,
        "The `destination` memref must be contiguous",
    ):
      self.module.operation.verify()

  def test_async_load_op_source_and_dest_must_have_same_element_type(self):
    with ir.InsertionPoint(self.module.body):
      func.FuncOp.from_py_func(
          ir.MemRefType.get([4, 8], ir.F32Type.get()),
          ir.MemRefType.get([4, 8], ir.F64Type.get()),
          ir.MemRefType.get([], ir.Type.parse("!mosaic_gpu.barrier")),
          ir.IntegerType.get_signless(32),
          ir.IntegerType.get_signless(32),
          name="async_load",
      )(
          lambda source, destination, barrier, *indices: mgpu.dialect.async_load(
              source,
              destination,
              barrier,
              indices,
              slice_lengths=[4, 8],
              collective=ir.ArrayAttr.get([]),
          )
      )

    with self.assertRaisesRegex(
        ir.MLIRError,
        "`source` and `destination` memrefs must have the same element",
    ):
      self.module.operation.verify()

  def test_async_load_op_slice_lengths_must_be_larger_than_minus_two(self):
    with ir.InsertionPoint(self.module.body):
      func.FuncOp.from_py_func(
          ir.MemRefType.get([4, 8], ir.F32Type.get()),
          ir.MemRefType.get([4, 8], ir.F32Type.get()),
          ir.MemRefType.get([], ir.Type.parse("!mosaic_gpu.barrier")),
          ir.IntegerType.get_signless(32),
          ir.IntegerType.get_signless(32),
          name="async_load",
      )(
          lambda source, destination, barrier, *indices: mgpu.dialect.async_load(
              source,
              destination,
              barrier,
              indices,
              slice_lengths=[-2, 8],
              collective=ir.ArrayAttr.get([]),
          )
      )

    with self.assertRaisesRegex(
        ir.MLIRError,
        "The `slice_lengths` attribute must not contain values less than -1",
    ):
      self.module.operation.verify()

  def test_async_load_op_source_and_dest_ranks_must_match_with_collapse(self):
    with ir.InsertionPoint(self.module.body):
      func.FuncOp.from_py_func(
          ir.MemRefType.get([1, 4, 8], ir.F32Type.get()),
          ir.MemRefType.get([4], ir.F32Type.get()),
          ir.MemRefType.get([], ir.Type.parse("!mosaic_gpu.barrier")),
          ir.IntegerType.get_signless(32),
          ir.IntegerType.get_signless(32),
          ir.IntegerType.get_signless(32),
          name="async_load",
      )(
          lambda source, destination, barrier, *indices: mgpu.dialect.async_load(
              source,
              destination,
              barrier,
              indices,
              slice_lengths=[-1, 4, 8],
              collective=ir.ArrayAttr.get([]),
          )
      )

    with self.assertRaisesRegex(
        ir.MLIRError,
        "`destination` plus the number of collapsed dimensions as indicated",
    ):
      self.module.operation.verify()

  def test_async_load_op_indices_size_must_match_source_rank(self):
    with ir.InsertionPoint(self.module.body):
      func.FuncOp.from_py_func(
          ir.MemRefType.get([4, 8], ir.F32Type.get()),
          ir.MemRefType.get([4, 8], ir.F32Type.get()),
          ir.MemRefType.get([], ir.Type.parse("!mosaic_gpu.barrier")),
          ir.IntegerType.get_signless(32),
          name="async_load",
      )(
          lambda source, destination, barrier, *indices: mgpu.dialect.async_load(
              source,
              destination,
              barrier,
              indices,
              slice_lengths=[4, 8],
              collective=ir.ArrayAttr.get([]),
          )
      )

    with self.assertRaisesRegex(
        ir.MLIRError,
        "The size of `indices` must be equal to the rank of `source`",
    ):
      self.module.operation.verify()

  def test_async_load_op_slice_lengths_size_must_match_source_rank(self):
    with ir.InsertionPoint(self.module.body):
      func.FuncOp.from_py_func(
          ir.MemRefType.get([4], ir.F32Type.get()),
          ir.MemRefType.get([4], ir.F32Type.get()),
          ir.MemRefType.get([], ir.Type.parse("!mosaic_gpu.barrier")),
          ir.IntegerType.get_signless(32),
          name="async_load",
      )(
          lambda source, destination, barrier, *indices: mgpu.dialect.async_load(
              source,
              destination,
              barrier,
              indices,
              slice_lengths=[4, 8],
              collective=ir.ArrayAttr.get([]),
          )
      )

    with self.assertRaisesRegex(
        ir.MLIRError,
        "The size of `slice_lengths` must be equal to the rank of `source`",
    ):
      self.module.operation.verify()

  def test_async_load_op_slice_collective_must_be_unique(self):
    with ir.InsertionPoint(self.module.body):
      i32 = ir.IntegerType.get_signless(32)
      func.FuncOp.from_py_func(
          ir.MemRefType.get([4], ir.F32Type.get()),
          ir.MemRefType.get([4], ir.F32Type.get()),
          ir.MemRefType.get([], ir.Type.parse("!mosaic_gpu.barrier")),
          i32,
          name="async_load",
      )(
          lambda source, destination, barrier, *indices: mgpu.dialect.async_load(
              source,
              destination,
              barrier,
              indices,
              slice_lengths=[4],
              collective=ir.ArrayAttr.get([
                  ir.IntegerAttr.get(i32, mgpu.dialect.Dimension.x),
                  ir.IntegerAttr.get(i32, mgpu.dialect.Dimension.x),
              ]),
          )
      )

    with self.assertRaisesRegex(
        ir.MLIRError,
        "The `collective` attribute must not contain duplicate dimensions",
    ):
      self.module.operation.verify()

  def test_async_store_op_source_must_be_contiguous(self):
    with ir.InsertionPoint(self.module.body):
      func.FuncOp.from_py_func(
          ir.MemRefType.get(
              [4, 8],
              ir.F32Type.get(),
              layout=ir.Attribute.parse("strided<[16, 1]>"),
          ),
          ir.MemRefType.get([4, 8], ir.F32Type.get()),
          ir.IntegerType.get_signless(32),
          ir.IntegerType.get_signless(32),
          name="async_store",
      )(
          lambda source, destination, *indices: mgpu.dialect.async_store(
              source,
              destination,
              indices,
              slice_lengths=[4, 8],
          )
      )

    with self.assertRaisesRegex(
        ir.MLIRError,
        "The `source` memref must be contiguous",
    ):
      self.module.operation.verify()

  def test_async_store_op_source_and_dest_must_have_same_element_type(self):
    with ir.InsertionPoint(self.module.body):
      func.FuncOp.from_py_func(
          ir.MemRefType.get([4, 8], ir.F32Type.get()),
          ir.MemRefType.get([4, 8], ir.F64Type.get()),
          ir.IntegerType.get_signless(32),
          ir.IntegerType.get_signless(32),
          name="async_store",
      )(
          lambda source, destination, *indices: mgpu.dialect.async_store(
              source,
              destination,
              indices,
              slice_lengths=[4, 8],
          )
      )

    with self.assertRaisesRegex(
        ir.MLIRError,
        "`source` and `destination` memrefs must have the same element",
    ):
      self.module.operation.verify()

  def test_async_store_op_slice_lengths_must_be_larger_than_minus_two(self):
    with ir.InsertionPoint(self.module.body):
      func.FuncOp.from_py_func(
          ir.MemRefType.get([4, 8], ir.F32Type.get()),
          ir.MemRefType.get([4, 8], ir.F32Type.get()),
          ir.IntegerType.get_signless(32),
          ir.IntegerType.get_signless(32),
          name="async_store",
      )(
          lambda source, destination, *indices: mgpu.dialect.async_store(
              source,
              destination,
              indices,
              slice_lengths=[-2, 8],
          )
      )

    with self.assertRaisesRegex(
        ir.MLIRError,
        "The `slice_lengths` attribute must not contain values less than -1",
    ):
      self.module.operation.verify()

  def test_async_store_op_source_and_dest_ranks_must_match_with_collapse(self):
    with ir.InsertionPoint(self.module.body):
      func.FuncOp.from_py_func(
          ir.MemRefType.get([4], ir.F32Type.get()),
          ir.MemRefType.get([1, 4, 8], ir.F32Type.get()),
          ir.IntegerType.get_signless(32),
          ir.IntegerType.get_signless(32),
          ir.IntegerType.get_signless(32),
          name="async_store",
      )(
          lambda source, destination, *indices: mgpu.dialect.async_store(
              source,
              destination,
              indices,
              slice_lengths=[-1, 4, 8],
          )
      )

    with self.assertRaisesRegex(
        ir.MLIRError,
        "`source` plus the number of collapsed dimensions as indicated",
    ):
      self.module.operation.verify()

  def test_async_store_op_indices_size_must_match_destination_rank(self):
    with ir.InsertionPoint(self.module.body):
      func.FuncOp.from_py_func(
          ir.MemRefType.get([4, 8], ir.F32Type.get()),
          ir.MemRefType.get([4, 8], ir.F32Type.get()),
          ir.IntegerType.get_signless(32),
          name="async_store",
      )(
          lambda source, destination, *indices: mgpu.dialect.async_store(
              source,
              destination,
              indices,
              slice_lengths=[4, 8],
          )
      )

    with self.assertRaisesRegex(
        ir.MLIRError,
        "The size of `indices` must be equal to the rank of `destination`",
    ):
      self.module.operation.verify()

  def test_async_store_op_slice_lengths_size_must_match_source_rank(self):
    with ir.InsertionPoint(self.module.body):
      func.FuncOp.from_py_func(
          ir.MemRefType.get([4], ir.F32Type.get()),
          ir.MemRefType.get([4], ir.F32Type.get()),
          ir.IntegerType.get_signless(32),
          name="async_store",
      )(
          lambda source, destination, *indices: mgpu.dialect.async_store(
              source,
              destination,
              indices,
              slice_lengths=[4, 8],
          )
      )

    with self.assertRaisesRegex(
        ir.MLIRError,
        "The size of `slice_lengths` must be equal to the rank of"
        " `destination`",
    ):
      self.module.operation.verify()

  def test_wgmma_types_match(self):
    with ir.InsertionPoint(self.module.body):
      func.FuncOp.from_py_func(
          ir.VectorType.get([128, 160], ir.BF16Type.get()),
          ir.MemRefType.get([128, 128], ir.F16Type.get()),
          ir.MemRefType.get([128, 160], ir.BF16Type.get()),
          name="wgmma",
      )(mgpu.dialect.wgmma)

    with self.assertRaisesRegex(
        ir.MLIRError,
        "The `a` and `b` inputs must have the same element type.",
    ):
      self.module.operation.verify()

  def test_wgmma_a_rank_is_2(self):
    with ir.InsertionPoint(self.module.body):
      func.FuncOp.from_py_func(
          ir.VectorType.get([128, 160], ir.BF16Type.get()),
          ir.MemRefType.get([3, 128, 128], ir.BF16Type.get()),
          ir.MemRefType.get([128, 160], ir.BF16Type.get()),
          name="wgmma",
      )(mgpu.dialect.wgmma)

    with self.assertRaisesRegex(
        ir.MLIRError,
        "The `a` input must have rank 2.",
    ):
      self.module.operation.verify()

  def test_wgmma_b_rank_is_2(self):
    with ir.InsertionPoint(self.module.body):
      func.FuncOp.from_py_func(
          ir.VectorType.get([128, 160], ir.BF16Type.get()),
          ir.MemRefType.get([128, 128], ir.BF16Type.get()),
          ir.MemRefType.get([2, 128, 160], ir.BF16Type.get()),
          name="wgmma",
      )(mgpu.dialect.wgmma)

    with self.assertRaisesRegex(
        ir.MLIRError,
        "The `b` input must have rank 2.",
    ):
      self.module.operation.verify()

  def test_wgmma_acc_rank_is_2(self):
    with ir.InsertionPoint(self.module.body):
      func.FuncOp.from_py_func(
          ir.VectorType.get([2, 128, 160], ir.BF16Type.get()),
          ir.MemRefType.get([128, 128], ir.BF16Type.get()),
          ir.MemRefType.get([128, 160], ir.BF16Type.get()),
          name="wgmma",
      )(mgpu.dialect.wgmma)

    with self.assertRaisesRegex(
        ir.MLIRError,
        "The accumulator must have rank 2.",
    ):
      self.module.operation.verify()

  def test_wgmma_acc_m_dim_not_multiple_of_64(self):
    with ir.InsertionPoint(self.module.body):
      func.FuncOp.from_py_func(
          ir.VectorType.get([127, 160], ir.BF16Type.get()),
          ir.MemRefType.get([128, 128], ir.BF16Type.get()),
          ir.MemRefType.get([128, 160], ir.BF16Type.get()),
          name="wgmma",
      )(mgpu.dialect.wgmma)

    with self.assertRaisesRegex(
        ir.MLIRError,
        r"accumulator.*must be a multiple of 64",
    ):
      self.module.operation.verify()

  def test_wgmma_acc_m_not_equal_to_a_m_dim(self):
    with ir.InsertionPoint(self.module.body):
      func.FuncOp.from_py_func(
          ir.VectorType.get([256, 160], ir.BF16Type.get()),
          ir.MemRefType.get([512, 128], ir.BF16Type.get()),
          ir.MemRefType.get([128, 160], ir.BF16Type.get()),
          name="wgmma",
      )(mgpu.dialect.wgmma)

    with self.assertRaisesRegex(
        ir.MLIRError,
        r"accumulator's first dimension 256 must be equal to.*`a`",
    ):
      self.module.operation.verify()

  def test_wgmma_a_k_dim_not_equal_to_b_k_dim(self):
    with ir.InsertionPoint(self.module.body):
      func.FuncOp.from_py_func(
          ir.VectorType.get([128, 160], ir.BF16Type.get()),
          ir.MemRefType.get([128, 128], ir.BF16Type.get()),
          ir.MemRefType.get([160, 160], ir.BF16Type.get()),
          name="wgmma",
      )(mgpu.dialect.wgmma)

    with self.assertRaisesRegex(
        ir.MLIRError,
        r"`a`'s contracting dimension 128 must be equal to one of.*`b`",
    ):
      self.module.operation.verify()

  def test_wgmma_b_n_dim_not_equal_to_acc_n_dim(self):
    with ir.InsertionPoint(self.module.body):
      func.FuncOp.from_py_func(
          ir.VectorType.get([128, 160], ir.BF16Type.get()),
          ir.MemRefType.get([128, 128], ir.BF16Type.get()),
          ir.MemRefType.get([128, 192], ir.BF16Type.get()),
          name="wgmma",
      )(mgpu.dialect.wgmma)

    with self.assertRaisesRegex(
        ir.MLIRError,
        r"`b`'s non-contracting dimension 192 must be equal to the",
    ):
      self.module.operation.verify()

  def test_tiled_layout_attr_parsing(self):
    with ir.InsertionPoint(self.module.body):
      for layout in (
          mgpu.WGMMA_LAYOUT,
          mgpu.WGMMA_ROW_LAYOUT,
          mgpu.WGMMA_COL_LAYOUT,
          mgpu.WGMMA_TRANSPOSED_LAYOUT,
      ):
        attr = layouts.to_tiled_layout_attr(layout)
        parsed_layout = layouts.from_tiled_layout_attr(attr)
        self.assertEqual(layout, parsed_layout)

  def test_broadcast_in_dim_ok(self):
    with ir.InsertionPoint(self.module.body):
      func.FuncOp.from_py_func(
          ir.VectorType.get([64], ir.F32Type.get()),
          name="broadcast_in_dim",
      )(
          lambda operand: mgpu.dialect.broadcast_in_dim(
              ir.VectorType.get([64, 64], ir.F32Type.get()),
              operand,
              broadcast_dimensions=[0],
          )
      )

    self.assertTrue(self.module.operation.verify())

  def test_broadcast_in_dim_no_0d(self):
    with ir.InsertionPoint(self.module.body):
      func.FuncOp.from_py_func(
          ir.VectorType.get([], ir.F32Type.get()),
          name="broadcast_in_dim",
      )(
          lambda operand: mgpu.dialect.broadcast_in_dim(
              ir.VectorType.get([64], ir.F32Type.get()),
              operand,
              broadcast_dimensions=[],
          )
      )

    with self.assertRaisesRegex(
        ir.MLIRError,
        r"The input vector must have rank > 0",
    ):
      self.module.operation.verify()

  def test_broadcast_in_dim_no_input_larger_than_output(self):
    with ir.InsertionPoint(self.module.body):
      func.FuncOp.from_py_func(
          ir.VectorType.get([64, 64], ir.F32Type.get()),
          name="broadcast_in_dim",
      )(
          lambda operand: mgpu.dialect.broadcast_in_dim(
              ir.VectorType.get([64], ir.F32Type.get()),
              operand,
              broadcast_dimensions=[],
          )
      )

    with self.assertRaisesRegex(
        ir.MLIRError,
        r"rank of the input vector must be smaller",
    ):
      self.module.operation.verify()

  def test_broadcast_in_dim_too_many_dims(self):
    with ir.InsertionPoint(self.module.body):
      func.FuncOp.from_py_func(
          ir.VectorType.get([64], ir.F32Type.get()),
          name="broadcast_in_dim",
      )(
          lambda operand: mgpu.dialect.broadcast_in_dim(
              ir.VectorType.get([64, 64], ir.F32Type.get()),
              operand,
              broadcast_dimensions=[0, 1],
          )
      )

    with self.assertRaisesRegex(
        ir.MLIRError,
        r"size of the `broadcast_dimensions` attribute must be",
    ):
      self.module.operation.verify()

  def test_broadcast_in_dim_dim_oob(self):
    with ir.InsertionPoint(self.module.body):
      func.FuncOp.from_py_func(
          ir.VectorType.get([64], ir.F32Type.get()),
          name="broadcast_in_dim",
      )(
          lambda operand: mgpu.dialect.broadcast_in_dim(
              ir.VectorType.get([64, 64], ir.F32Type.get()),
              operand,
              broadcast_dimensions=[2],
          )
      )

    with self.assertRaisesRegex(
        ir.MLIRError,
        r"must be in the range \[0, result.shape.rank",
    ):
      self.module.operation.verify()

  def test_broadcast_in_dim_dim_transpose(self):
    with ir.InsertionPoint(self.module.body):
      func.FuncOp.from_py_func(
          ir.VectorType.get([64, 64, 64, 64], ir.F32Type.get()),
          name="broadcast_in_dim",
      )(
          lambda operand: mgpu.dialect.broadcast_in_dim(
              ir.VectorType.get([64, 64, 64, 64], ir.F32Type.get()),
              operand,
              broadcast_dimensions=[0, 1, 3, 2],
          )
      )

    with self.assertRaisesRegex(
        ir.MLIRError,
        r"`broadcast_dimensions` attribute must be strictly increasing",
    ):
      self.module.operation.verify()

  def test_custom_primitive_op_args_must_match_args_of_terminator(self):
    def kernel():
      shape = (128,)
      elt_ty = ir.F32Type.get()
      ty = ir.VectorType.get(shape, elt_ty)
      out_layouts = ir.ArrayAttr.get([
          layouts.to_layout_attr(mgpu.WGStridedFragLayout.from_shaped_type(ty))
      ])

      op = mgpu.dialect.CustomPrimitiveOp(
          result=[ty],
          operands_=[],
          in_layouts=[],
          in_transforms=[],
          out_layouts=out_layouts,
      )
      block = op.body.blocks.append()
      with ir.InsertionPoint(block):
        v = llvm.mlir_undef(ir.VectorType.get([256], ir.F32Type.get()))
        mgpu.dialect.ReturnOp(operands_=[v])

    with ir.InsertionPoint(self.module.body):
      func.FuncOp.from_py_func(name="kernel")(kernel)

    with self.assertRaisesRegex(
        ir.MLIRError,
        r"type of return operand 0 \('vector<256xf32>'\) doesn't match the"
        r" result type \('vector<128xf32>'\) in custom_primitive",
    ):
      self.module.operation.verify()


class DialectLoweringTest(MosaicGpuTest):

  def test_lowering_removes_mosaic_gpu_ops(self):
    with ir.InsertionPoint(self.module.body):
      mgpu.dialect.initialize_barrier(
          ir.MemRefType.get((1, 2), ir.Type.parse("!mosaic_gpu.barrier")),
          llvm.UndefOp(workgroup_ptr_ty()),
          arrival_count=1,
      )
    mgpu.lower_mgpu_dialect(self.module, None)

    self.assertEmpty(
        list(filter(is_mosaic_gpu_op, self.module.body.operations))
    )

  def test_lowering_traverses_regions_correctly(self):
    with ir.InsertionPoint(self.module.body):
      bool_type = ir.IntegerType.get_signless(1)
      cst_true = arith.constant(bool_type, ir.IntegerAttr.get(bool_type, 1))
      if_op = scf.IfOp(cst_true)
      with ir.InsertionPoint(if_op.then_block):
        mgpu.dialect.initialize_barrier(
            ir.MemRefType.get((1, 2), ir.Type.parse("!mosaic_gpu.barrier")),
            llvm.UndefOp(workgroup_ptr_ty()),
            arrival_count=1,
        )
        scf.yield_([])
    mgpu.lower_mgpu_dialect(self.module, None)

    self.assertEmpty(
        list(filter(is_mosaic_gpu_op, if_op.then_block.operations))
    )

  def test_initialize_barrier_op_lowering_rule(self):
    shape = (3, 4)
    num_shape_elements = shape[0] * shape[1]
    arrival_count = 1337

    with ir.InsertionPoint(self.module.body):
      barriers_ref = mgpu.dialect.initialize_barrier(
          ir.MemRefType.get(shape, ir.Type.parse("!mosaic_gpu.barrier")),
          llvm.UndefOp(workgroup_ptr_ty()),
          arrival_count=arrival_count,
      )
      # Add a user for barriers_ref to make sure that the lowering keeps types
      # consistent.
      memref.copy(barriers_ref, barriers_ref)

    self.assertTrue(self.module.operation.verify())
    mgpu.lower_mgpu_dialect(self.module, None)
    self.assertTrue(self.module.operation.verify())

    all_mbarrier_init_shared_ops = find_if(
        self.module,
        lambda op: op.name == nvvm.MBarrierInitSharedOp.OPERATION_NAME,
    )

    # One nvvm.mbarrier_init_shared is issued per barrier.
    self.assertLen(all_mbarrier_init_shared_ops, num_shape_elements)

    # Each barrier has its count equal to the arrival count times the
    # warpgroup size.
    for op in all_mbarrier_init_shared_ops:
      count = op.count.owner.opview
      self.assertIsInstance(count, arith.ConstantOp)
      self.assertEqual(
          count.literal_value, arrival_count * mgpu_utils.WARPGROUP_SIZE
      )

  def test_lowering_vector_op_without_layout_fails(self):
    shape = (3, 4)
    elt_ty = ir.BF16Type.get()
    with ir.InsertionPoint(self.module.body):
      ref = llvm.mlir_undef(ir.MemRefType.get(shape, elt_ty))
      zero_index = arith.constant(ir.IndexType.get(), 0)
      ty = ir.VectorType.get(shape, elt_ty)
      vector.load(ty, ref, [zero_index, zero_index])
    with self.assertRaisesRegex(
        ValueError, "missing a layout and can not be lowered"
    ):
      mgpu.lower_mgpu_dialect(self.module, None)

  def test_lowering_eliminates_layouts(self):
    shape = (4, 128)
    elt_ty = ir.BF16Type.get()
    with ir.InsertionPoint(self.module.body):
      ref = llvm.mlir_undef(ir.MemRefType.get(shape, elt_ty))
      zero_index = arith.constant(ir.IndexType.get(), 0)
      ty = ir.VectorType.get(shape, elt_ty)
      load = vector.load(ty, ref, [zero_index, zero_index])
      load.owner.attributes["out_layouts"] = ir.ArrayAttr.get([
          layouts.to_layout_attr(mgpu.WGStridedFragLayout.from_shaped_type(ty))
      ])

    mgpu.lower_mgpu_dialect(self.module, None)

    all_ops_with_layouts = find_if(
        self.module,
        lambda op: (
            "out_layouts" in op.attributes or "in_layouts" in op.attributes
        ),
    )
    self.assertEmpty(all_ops_with_layouts)

  def test_lowering_splat_constant(self):
    cst = None
    elt_ty = ir.BF16Type.get()

    def body():
      vec_ty = ir.VectorType.get((16, 8), elt_ty)
      zero = ir.FloatAttr.get(elt_ty, 0)
      nonlocal cst
      cst = arith.ConstantOp(
          vec_ty, ir.DenseElementsAttr.get_splat(vec_ty, zero)
      )
      cst.attributes["out_layouts"] = ir.ArrayAttr.get([
          layouts.to_layout_attr(
              mgpu.WGStridedFragLayout.from_shaped_type(vec_ty)
          )
      ])

    with ir.InsertionPoint(self.module.body):
      func.FuncOp.from_py_func()(body)

    mgpu.lower_mgpu_dialect(self.module, None)

    cst_ops = find_if(
        self.module,
        lambda op: isinstance(op, arith.ConstantOp),
    )
    self.assertLen(cst_ops, 1)
    self.assertEqual(cst_ops[0].result.type, elt_ty)

  def test_lowering_vector_load_and_store_ops(self):
    shape = (8, 128)
    elt_ty = ir.BF16Type.get()
    with ir.InsertionPoint(self.module.body):
      ref = llvm.mlir_undef(ir.MemRefType.get(shape, elt_ty))
      zero_index = arith.constant(ir.IndexType.get(), 0)
      ty = ir.VectorType.get(shape, elt_ty)
      array = vector.load(ty, ref, [zero_index, zero_index])
      vector.store(array, ref, [zero_index, zero_index])

    mgpu.infer_layout(self.module)
    mgpu.lower_mgpu_dialect(self.module, None)

    all_loads = find_if(
        self.module,
        lambda op: isinstance(op, vector.LoadOp),
    )
    all_stores = find_if(
        self.module,
        lambda op: isinstance(op, vector.StoreOp),
    )

    # The shape is (8, 128). Assuming a single warpgroup (128 threads), we
    # expect each thread to load 8 elements---with two vectorized loads of size
    # 8 bytes.
    self.assertLen(all_loads, 2)
    self.assertLen(all_stores, 2)

    def check_type(ty: ir.Type):
      self.assertTrue(ir.VectorType.get((4,), elt_ty).isinstance(ty))

    load1, load2, *_ = all_loads  # Variadic unpacking to silence linter.
    check_type(load1.result.type)
    check_type(load2.result.type)

    store1, store2, *_ = all_stores  # Variadic unpacking to silence linter.
    check_type(store1.valueToStore.type)
    check_type(store2.valueToStore.type)

  def test_lowering_for(self):
    shape = (4, 128)
    i32 = ir.IntegerType.get_signless(32)
    vec_ty = ir.VectorType.get(shape, i32)
    splat_layout_attr = layouts.to_layout_attr(mgpu.WGSplatFragLayout(shape))
    strided_layout_attr = layouts.to_layout_attr(
        mgpu.WGStridedFragLayout.from_shaped_type(vec_ty)
    )
    with ir.InsertionPoint(self.module.body):
      i1 = arith.constant(ir.IndexType.get(), 1)
      c1 = arith.constant(i32, 1)
      splat = vector.SplatOp(
          ir.VectorType.get(shape, i32), arith.constant(i32, 1234),
      )
      splat.attributes["out_layouts"] = ir.ArrayAttr.get([
          splat_layout_attr
      ])
      ptr = llvm.mlir_undef(ir.Type.parse("!llvm.ptr"))
      ref = mgpu_utils.ptr_as_memref(ptr, ir.MemRefType.get(shape, i32))
      i0 = arith.constant(ir.IndexType.get(), 0)
      other_vec = vector.LoadOp(vec_ty, ref, [i0, i0])
      other_vec.attributes["out_layouts"] = ir.ArrayAttr.get([strided_layout_attr])
      for_op = scf.ForOp(i1, i1, i1, [c1, splat.result])
      for_op.attributes["in_layouts"] = ir.ArrayAttr.get([strided_layout_attr])
      for_op.attributes["out_layouts"] = ir.ArrayAttr.get([strided_layout_attr])
      with ir.InsertionPoint(for_op.body):
        i, int_carry, vec_carry = for_op.body.arguments
        new_int_carry = arith.addi(int_carry, arith.index_castui(i32, i))
        new_vec_carry = arith.AddIOp(vec_carry, other_vec)
        new_vec_carry.attributes["in_layouts"] = ir.ArrayAttr.get([strided_layout_attr] * 2)
        new_vec_carry.attributes["out_layouts"] = ir.ArrayAttr.get([strided_layout_attr])
        yield_op = scf.YieldOp([new_int_carry, new_vec_carry])
        yield_op.attributes["in_layouts"] = ir.ArrayAttr.get([strided_layout_attr])

    mgpu.lower_mgpu_dialect(self.module, None)
    self.module.operation.verify()
    [for_op] = find_if(self.module, lambda op: isinstance(op, scf.ForOp))
    result_types = [r.type for r in for_op.results]
    reg_vec_ty = ir.VectorType.get((2,), i32)
    self.assertSequenceEqual(result_types, [i32, reg_vec_ty, reg_vec_ty])

  def test_lowering_slice_smem_op(self):
    shift = 1234
    offset = None

    def body():
      nonlocal offset
      i32 = ir.IntegerType.get_signless(32)
      memref_ty = ir.MemRefType.get((4, 32), i32, memory_space=mgpu_utils.smem())
      offset = arith.constant(i32, shift)
      op = mgpu.dialect.SliceSMEMOp(memref_ty, offset)
      op.attributes["out_transforms"] = ir.ArrayAttr.get([ir.ArrayAttr.get([])])

    with ir.InsertionPoint(self.module.body):
      func.FuncOp.from_py_func()(body)

    mgpu.lower_mgpu_dialect(self.module, None)
    # Avoid making a change detector, only validate that lowering runs as
    # expected.
    self.assertEmpty(
        find_if(
            self.module, lambda op: isinstance(op, mgpu.dialect.SliceSMEMOp)
        )
    )

  @parameterized.parameters(
      (arith.ExtFOp, jnp.bfloat16, jnp.float32),
      (arith.ExtSIOp, jnp.int16, jnp.int32),
      (arith.ExtUIOp, jnp.int16, jnp.uint32),
      (arith.FPToSIOp, jnp.float32, jnp.int32),
      (arith.FPToUIOp, jnp.float32, jnp.uint32),
      (arith.SIToFPOp, jnp.int16, jnp.float32),
      (arith.TruncFOp, jnp.float32, jnp.float16),
      (arith.TruncIOp, jnp.int32, jnp.int16),
      (arith.UIToFPOp, jnp.uint32, jnp.float32),
  )
  def test_lower_conversion_op_lowers_to_same_op(self, op, in_dtype, out_dtype):
    shape = (4, 32)

    with ir.InsertionPoint(self.module.body):
      scalar_in_ty = mgpu_utils.dtype_to_ir_type(in_dtype)
      scalar_out_ty = mgpu_utils.dtype_to_ir_type(out_dtype)
      in_ty = ir.VectorType.get(shape, scalar_in_ty)
      out_ty = ir.VectorType.get(shape, scalar_out_ty)
      if ir.IntegerType.isinstance(scalar_in_ty):
        zero = ir.IntegerAttr.get(scalar_in_ty, 0)
      else:
        zero = ir.FloatAttr.get(scalar_in_ty, 0)
      splat_zero = arith.ConstantOp(
          in_ty, ir.DenseElementsAttr.get_splat(in_ty, zero)
      )
      op(out_ty, splat_zero)

    mgpu.infer_layout(self.module)
    mgpu.lower_mgpu_dialect(self.module, None)

    conversion_ops = find_if(self.module, lambda o: isinstance(o, op))
    # This is a splat, so we expect a single conversion op involving a scalar
    # after lowering.
    self.assertLen(conversion_ops, 1)
    self.assertEqual(conversion_ops[0].result.type, scalar_out_ty)

  @parameterized.parameters(
      (True, False, False),
      (False, True, False),
      (False, False, True),
  )
  def test_custom_primitive_op_must_have_number_of_annotations_matching_operands_and_results(
      self, omit_in_layouts, omit_in_transforms, omit_out_layouts
  ):
    vec_ty = ir.VectorType.get((4, 32), ir.BF16Type.get())
    out_layouts = [
        layouts.to_layout_attr(
            mgpu.WGStridedFragLayout.from_shaped_type(vec_ty)
        )
    ]
    in_layouts = out_layouts * 2
    in_transforms = [
        ir.ArrayAttr.get([mgpu.dialect.SwizzleTransformAttr.get(128)])
    ]

    in_layouts = [] if omit_in_layouts else in_layouts
    in_transforms = [] if omit_in_transforms else in_transforms
    out_layouts = [] if omit_out_layouts else out_layouts

    def body(vec1, vec2, ref):
      mgpu.dialect.custom_primitive(
          [vec_ty], [vec1, vec2, ref], in_layouts, in_transforms, out_layouts
      )

    with ir.InsertionPoint(self.module.body):
      ref_ty = ir.MemRefType.get((4, 32), ir.BF16Type.get(), memory_space=mgpu_utils.smem())
      func.FuncOp.from_py_func(vec_ty, vec_ty, ref_ty)(body)

    if omit_in_layouts:
      error = "layout for each vector operand"
    elif omit_in_transforms:
      error = "transforms for each memref operand in smem"
    else:
      assert omit_out_layouts
      error = "layout for each result"

    with self.assertRaisesRegex(ir.MLIRError, error):
      self.module.operation.verify()

  def test_memref_transforms_with_transpose(self):
    with ir.InsertionPoint(self.module.body):
      ty_in = ir.MemRefType.get(
          (64, 128),
          ir.BF16Type.get(),
          memory_space=mgpu_utils.smem(),
      )
      ref = memref.alloc(ty_in, [], [])

      ref = mgpu_utils.memref_transpose(ref, (1, 0))
      # This tiling is applied to the transposed memref.
      transforms = [mgpu.TileTransform(tiling=(16, 32))]

      ref_transformed = lowering.reinterpret_smem_ref(ref, transforms)
      ty_transformed = ir.MemRefType(ref_transformed.type)
      self.assertEqual(ty_transformed.shape, [8, 2, 16, 32])
      strides, _ = ty_transformed.get_strides_and_offset()
      self.assertEqual(strides, [512, 4096, 1, 16])


if __name__ == "__main__":
  parameterized.absltest.main(testLoader=jtu.JaxTestLoader())
