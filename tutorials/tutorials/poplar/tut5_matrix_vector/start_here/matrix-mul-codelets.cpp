// Copyright (c) 2018 Graphcore Ltd. All rights reserved.

#include <poplar/Vertex.hpp>

using namespace poplar;

// This file contains the definitions of the vertex types used by the
// matrix-vector multiplication example. The vertex type provides a description
// of how individual tasks on the IPU will perform computation.

// A vertex type to perform a dot product calculation.
class DotProductVertex : public Vertex {
public:
  // These two inputs read a vector of values (that is, an ordered, contiguous
  // set of values in memory) from the graph.
  Input<Vector<float>> a;
  Input<Vector<float>> b;

  // The output is to a single scalar value in the graph.
  Output<float> out;

  // The compute method performs the dot product between inputs 'a' and
  // 'b' and stores the result in 'out'.
  bool compute() {
    /* #### TO DO (1) #### */
    return true;
  }
};
