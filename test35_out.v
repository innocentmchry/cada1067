module test35 (
  in0,
  in1,
  in2,
  _gc_ctrl,
  out0,
  out1,
  out2
);

  input wire in0;
  input wire in1;
  input wire in2;
  input wire _gc_ctrl;
  output wire out0;
  output wire out1;
  output wire out2;

  wire w0;
  wire w1;
  wire w2;

  and    U_gc__buf0           (w0, in0, _gc_ctrl);
  and    U23_gc__buf          (w1, in1, _gc_ctrl);
  and    buf_gc__stage3       (w2, in2, _gc_ctrl);
  buf    out_drv0             (out0, w0);
  buf    out_drv1             (out1, w1);
  buf    out_drv2             (out2, w2);

endmodule
