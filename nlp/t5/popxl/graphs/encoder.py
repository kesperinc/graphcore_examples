# Copyright (c) 2023 Graphcore Ltd. All rights reserved.
import popxl
import popxl_addons as addons
from popxl_addons.graph import GraphWithNamedArgs
from popxl_addons.variable_factory import NamedVariableFactories
from popxl_addons.named_tensors import NamedTensors
from popxl_addons.transforms.batch_serialisation import (
    batch_serialise_fwd_and_grad,
    RemoteHandle,
)
from popxl_addons.rts import (
    all_gather_replica_sharded_graph,
    reduce_replica_sharded_graph,
)
from popxl_addons.remote import (
    named_variable_buffers,
    load_remote_graph,
    store_remote_graph,
)

from config import T5Config
from modelling.encoder import T5EncoderBlockTP, T5EncoderHead
from graphs.graphs import (
    Graphs,
    optimizer_graphs,
    get_rts_groups,
    use_io_tiles,
)


def create_first_encoder_layer_graph(config: T5Config, optimizer: addons.Module, *args, **kwargs):
    layer = Graphs()

    dp_group = popxl.gcg().ir.replica_grouping(
        stride=config.execution.tensor_parallel, group_size=config.execution.data_parallel
    )

    # Create Graphs for computing forward, gradient and optimizer
    fwd_facts, layer.fwd = T5EncoderBlockTP(config).create_graph(*args, **kwargs)
    required_grads = (layer.fwd.graph.inputs[0],)

    called_graphs_grad_info = {}
    if config.execution.attention_serialisation > 1:
        # Optimisation to recompute each blk separately
        assert len(layer.fwd.graph.called_graphs) == 1, "expected exactly 1 called graph by first encoder layer fwd"
        blk_graph = next(g for g in layer.fwd.graph.called_graphs if "attention_block" in g.name)
        blk_graph = GraphWithNamedArgs(blk_graph)
        grad_blk_graph = addons.transforms.autodiff(blk_graph, grads_required=blk_graph.graph.inputs[:-2])
        grad_blk_graph = addons.transforms.recompute_graph(grad_blk_graph)
        called_graphs_grad_info[blk_graph.graph] = grad_blk_graph.grad_graph_info

    grad_facts, layer.bwd = addons.autodiff_with_accumulation(
        layer.fwd,
        layer.fwd.args.tensors,
        grads_required=required_grads,
        called_graphs_grad_info=called_graphs_grad_info,
        replica_groupings=fwd_facts.replica_groupings,
    )

    popxl.transforms.decompose_sum(layer.bwd.graph)

    optim_args, layer.optim = optimizer_graphs(
        config,
        optimizer,
        layer.fwd.args,
        replica_groups=fwd_facts.replica_groupings,
        shard_groups=get_rts_groups(fwd_facts),
    )

    # Variables required
    layer.facts = NamedVariableFactories(fwd=fwd_facts, optim=optim_args)
    layer.grad_facts = grad_facts

    # Create remote buffers
    entries = 1  # it's only one layer
    buffer_facts = layer.facts.copy()
    buffer_facts.insert("bwd", grad_facts.copy())
    buffer_facts.bwd.pop("mean_accum_counter")

    rts_fwd_bwd_groups = get_rts_groups(buffer_facts)
    shard_over = {k: rg.group_size for k, rg in rts_fwd_bwd_groups.to_dict().items()}
    layer.buffers = named_variable_buffers(buffer_facts, entries, shard_over_dict=shard_over)

    # Create Graphs for loading/gathering/storing/reducing
    # Load fwd, bwd and optim
    layer._optim_fwd_load, layer._optim_fwd_load_names = load_remote_graph(layer.buffers, entries)

    buffers = layer.buffers.copy()
    buffers_grad = buffers.pop("bwd")
    # Store fwd and optim
    layer._optim_fwd_store = store_remote_graph(buffers, entries)
    # Store bwd
    layer._grad_store = store_remote_graph(buffers_grad, entries)
    # Load fwd
    layer._fwd_load, layer._fwd_load_names = load_remote_graph(layer.buffers.fwd, entries)
    layer._fwd_all_gather, layer._fwd_all_gather_names = all_gather_replica_sharded_graph(
        NamedTensors.pack(layer._fwd_load_names, layer._fwd_load.graph.outputs),
        replica_groups=rts_fwd_bwd_groups.fwd,
        use_io_tiles=use_io_tiles,
    )

    grad_accums = layer.bwd.args.copy()
    grad_accums.pop("mean_accum_counter")
    layer._grad_reduce, layer._grad_reduce_names = reduce_replica_sharded_graph(
        grad_accums, "mean", shard_groups=rts_fwd_bwd_groups.bwd, replica_group=dp_group, use_io_tiles=use_io_tiles
    )

    return layer


def create_encoder_block_graph(config: T5Config, optimizer: addons.Module, *args, **kwargs):
    layer = Graphs()

    dp_group = popxl.gcg().ir.replica_grouping(
        stride=config.execution.tensor_parallel, group_size=config.execution.data_parallel
    )

    # Create Graphs for computing forward, gradient and optimizer
    fwd_facts, layer.fwd = T5EncoderBlockTP(config).create_graph(*args, **kwargs)
    required_grads = (layer.fwd.graph.inputs[0],)

    called_graphs_grad_info = {}
    if config.execution.attention_serialisation > 1:
        # Optimisation to recompute each blk separately
        assert len(layer.fwd.graph.called_graphs) == 1, "expected exactly 1 called graph by encoder layer fwd"
        blk_graph = GraphWithNamedArgs(layer.fwd.graph.called_graphs[0])
        grad_blk_graph = addons.transforms.autodiff(blk_graph, grads_required=blk_graph.graph.inputs[:-2])
        grad_blk_graph = addons.transforms.recompute_graph(grad_blk_graph)
        called_graphs_grad_info[blk_graph.graph] = grad_blk_graph.grad_graph_info

    # Create the grad accumulator for the rel pos weight
    accums = list(layer.fwd.args.tensors) + [layer.fwd.graph.inputs[2]]  # own weights + rel pos weight
    replica_groupings = fwd_facts.replica_groupings
    replica_groupings.insert("rel_pos_weight", dp_group)
    grad_facts, layer.bwd = addons.autodiff_with_accumulation(
        layer.fwd,
        accums,
        grads_required=required_grads,
        called_graphs_grad_info=called_graphs_grad_info,
        replica_groupings=replica_groupings,
    )

    popxl.transforms.decompose_sum(layer.bwd.graph)

    optim_args, layer.optim = optimizer_graphs(
        config,
        optimizer,
        layer.fwd.args,
        replica_groups=fwd_facts.replica_groupings,
        shard_groups=get_rts_groups(fwd_facts),
    )

    # Variables required
    layer.facts = NamedVariableFactories(fwd=fwd_facts, optim=optim_args)
    # Remove the rel pos weight from the grad accumulators: we'll create it elsewhere
    grad_facts.accum.pop("rel_pos_weight")
    layer.grad_facts = grad_facts

    # Create remote buffers
    entries = config.model.layers - 1  # -1 because we create the first layer elsewhere
    buffer_facts = layer.facts.copy()
    buffer_facts.insert("bwd", grad_facts.copy())
    buffer_facts.bwd.pop("mean_accum_counter")

    rts_fwd_bwd_groups = get_rts_groups(buffer_facts)
    shard_over = {k: rg.group_size for k, rg in rts_fwd_bwd_groups.to_dict().items()}
    layer.buffers = named_variable_buffers(buffer_facts, entries, shard_over_dict=shard_over)

    # Create Graphs for loading/gathering/storing/reducing
    # Load fwd, bwd and optim
    layer._optim_fwd_load, layer._optim_fwd_load_names = load_remote_graph(layer.buffers, entries)

    buffers = layer.buffers.copy()
    buffers_grad = buffers.pop("bwd")
    # Store fwd and optim
    layer._optim_fwd_store = store_remote_graph(buffers, entries)
    # Store bwd
    layer._grad_store = store_remote_graph(buffers_grad, entries)

    layer._fwd_load, layer._fwd_load_names = load_remote_graph(layer.buffers.fwd, entries)
    layer._fwd_all_gather, layer._fwd_all_gather_names = all_gather_replica_sharded_graph(
        NamedTensors.pack(layer._fwd_load_names, layer._fwd_load.graph.outputs),
        replica_groups=rts_fwd_bwd_groups.fwd,
        use_io_tiles=use_io_tiles,
    )

    grad_accums = layer.bwd.args.copy()
    grad_accums.pop("mean_accum_counter")
    # the rel pos weight is handled elsewhere
    grad_accums.accum.pop("rel_pos_weight")
    layer._grad_reduce, layer._grad_reduce_names = reduce_replica_sharded_graph(
        grad_accums, "mean", shard_groups=rts_fwd_bwd_groups.bwd, replica_group=dp_group, use_io_tiles=use_io_tiles
    )

    return layer


def create_encoder_head_graph(config: T5Config, optimizer: addons.Module, *args, **kwargs):
    layer = Graphs()

    dp_group = popxl.gcg().ir.replica_grouping(
        stride=config.execution.tensor_parallel, group_size=config.execution.data_parallel
    )

    # Create Graphs for computing forward, gradient and optimizer
    fwd_facts, layer.fwd = T5EncoderHead(config).create_graph(*args, **kwargs)
    required_grads = (layer.fwd.graph.inputs[0],)
    grad_facts, layer.bwd = addons.autodiff_with_accumulation(
        layer.fwd,
        layer.fwd.args.tensors,
        grads_required=required_grads,
        replica_groupings=fwd_facts.replica_groupings,
    )

    optim_args, layer.optim = optimizer_graphs(
        config,
        optimizer,
        layer.fwd.args,
        replica_groups=fwd_facts.replica_groupings,
        shard_groups=get_rts_groups(fwd_facts),
    )

    # Variables required
    layer.facts = NamedVariableFactories(fwd=fwd_facts, optim=optim_args)
    layer.grad_facts = grad_facts

    # Create remote buffers
    buffer_facts = layer.facts.copy()
    buffer_facts.insert("bwd", grad_facts.copy())
    buffer_facts.bwd.pop("mean_accum_counter")

    rts_fwd_bwd_groups = get_rts_groups(buffer_facts)
    shard_over = {k: rg.group_size for k, rg in rts_fwd_bwd_groups.to_dict().items()}
    layer.buffers = named_variable_buffers(buffer_facts, shard_over_dict=shard_over)

    # Create Graphs for loading/gathering/storing/reducing
    # Load fwd, bwd and optim
    layer._optim_fwd_load, layer._optim_fwd_load_names = load_remote_graph(layer.buffers)

    buffers = layer.buffers.copy()
    buffers_grad = buffers.pop("bwd")
    # Store fwd and optim
    layer._optim_fwd_store = store_remote_graph(buffers)
    # Store bwd
    layer._grad_store = store_remote_graph(buffers_grad)
    # Load fwd
    layer._fwd_load, layer._fwd_load_names = load_remote_graph(layer.buffers.fwd)
    layer._fwd_all_gather, layer._fwd_all_gather_names = all_gather_replica_sharded_graph(
        NamedTensors.pack(layer._fwd_load_names, layer._fwd_load.graph.outputs),
        replica_groups=rts_fwd_bwd_groups.fwd,
        use_io_tiles=use_io_tiles,
    )

    grad_accums = layer.bwd.args.copy()
    grad_accums.pop("mean_accum_counter")
    layer._grad_reduce, layer._grad_reduce_names = reduce_replica_sharded_graph(
        grad_accums, "mean", shard_groups=rts_fwd_bwd_groups.bwd, replica_group=dp_group, use_io_tiles=use_io_tiles
    )

    return layer


def first_encoder_layer_batch_serialise(
    config: T5Config,
    layer: Graphs,
    x_buffer: popxl.RemoteBuffer,
    dx_buffer: popxl.RemoteBuffer,
    mask_buffer: popxl.RemoteBuffer,
):
    tp = config.execution.tensor_parallel
    tp_group = popxl.gcg().ir.replica_grouping(stride=1, group_size=tp)
    x_shard_group = tp_group if x_buffer.meta_shape else popxl.gcg().ir.replica_grouping(group_size=1)
    dx_shard_group = tp_group if dx_buffer.meta_shape else popxl.gcg().ir.replica_grouping(group_size=1)

    fwd, bwd = batch_serialise_fwd_and_grad(
        layer.fwd,
        layer.bwd,
        layer.fwd.args,
        config.gradient_accumulation,
        load_handles={
            layer.fwd.graph.inputs[0]: RemoteHandle(x_buffer, 0, x_shard_group),
            layer.fwd.graph.inputs[1]: RemoteHandle(mask_buffer, None),
            layer.bwd.graph.inputs[0]: RemoteHandle(dx_buffer, 1, dx_shard_group),
        },
        store_streams={},
        store_buffers={
            layer.fwd.graph.outputs[0]: RemoteHandle(x_buffer, 1, x_shard_group),
            layer.bwd.graph.outputs[0]: RemoteHandle(dx_buffer, 0, dx_shard_group),
        },
        seed_input=layer.fwd.graph.inputs[2],
        rows=1,
        io_mode="io",
    )
    layer.fwd = fwd.graph
    layer.bwd = bwd.graph


def encoder_block_batch_serialise(
    config: T5Config,
    layer: Graphs,
    x_buffer: popxl.RemoteBuffer,
    dx_buffer: popxl.RemoteBuffer,
    mask_buffer: popxl.RemoteBuffer,
):
    tp = config.execution.tensor_parallel
    tp_group = popxl.gcg().ir.replica_grouping(stride=1, group_size=tp)
    x_shard_group = tp_group if x_buffer.meta_shape else popxl.gcg().ir.replica_grouping(group_size=1)
    dx_shard_group = tp_group if dx_buffer.meta_shape else popxl.gcg().ir.replica_grouping(group_size=1)

    fwd, bwd = batch_serialise_fwd_and_grad(
        layer.fwd,
        layer.bwd,
        layer.fwd.args,
        config.gradient_accumulation,
        load_handles={
            layer.fwd.graph.inputs[0]: RemoteHandle(x_buffer, 1, x_shard_group),
            layer.fwd.graph.inputs[1]: RemoteHandle(mask_buffer, None),
            layer.bwd.graph.inputs[0]: RemoteHandle(dx_buffer, 2, dx_shard_group),
        },
        store_streams={},
        store_buffers={
            layer.fwd.graph.outputs[0]: RemoteHandle(x_buffer, 2, x_shard_group),
            layer.bwd.graph.outputs[0]: RemoteHandle(dx_buffer, 1, dx_shard_group),
        },
        seed_input=layer.fwd.graph.inputs[3],
        rows=config.model.layers - 1,
        io_mode="io",
    )
    layer.fwd = fwd.graph
    layer.bwd = bwd.graph


def encoder_head_batch_serialise(
    config: T5Config,
    layer: Graphs,
    x_buffer: popxl.RemoteBuffer,
    dx_buffer: popxl.RemoteBuffer,
):
    tp = config.execution.tensor_parallel
    tp_group = popxl.gcg().ir.replica_grouping(stride=1, group_size=tp)
    x_shard_group = tp_group if x_buffer.meta_shape else popxl.gcg().ir.replica_grouping(group_size=1)
    dx_shard_group = tp_group if dx_buffer.meta_shape else popxl.gcg().ir.replica_grouping(group_size=1)

    fwd, bwd = batch_serialise_fwd_and_grad(
        layer.fwd,
        layer.bwd,
        layer.fwd.args,
        config.gradient_accumulation,
        load_handles={
            layer.fwd.graph.inputs[0]: RemoteHandle(x_buffer, config.model.layers, x_shard_group),
            layer.bwd.graph.inputs[0]: RemoteHandle(dx_buffer, config.model.layers + 1, dx_shard_group),
        },
        store_streams={},
        store_buffers={
            layer.fwd.graph.outputs[0]: RemoteHandle(x_buffer, config.model.layers + 1, x_shard_group),
            layer.bwd.graph.outputs[0]: RemoteHandle(dx_buffer, config.model.layers, dx_shard_group),
        },
        seed_input=layer.fwd.graph.inputs[1],
        io_mode="io",
    )
    layer.fwd = fwd.graph
    layer.bwd = bwd.graph
