module test35 (
  input wire in0,
  input wire in1,
  input wire in2,
  input wire _gc_ctrl,
  output wire out0,
  output wire out1,
  output wire out2
);
  wire w0, w1, w2;

  buf U_gc__buf0     (w0, in0);
  buf U23_gc__buf    (w1, in1);
  buf buf_gc__stage3 (w2, in2);
  buf out_drv0       (out0, w0);
  buf out_drv1       (out1, w1);
  buf out_drv2       (out2, w2);
endmodule
