module test8 (
  input wire in0,
  input wire n_aux0,
  input wire n_aux1,
  input wire n_aux2,
  output wire out3
);
  wire n_in0_n, n1, n2, n3;

  not  U1  (n_in0_n, in0);
  and  U5  (n1, n_in0_n, n_aux0);
  xor  U12 (n2, n1, n_aux1);
  nor  U18 (n3, n2, n_aux2);
  buf  U20 (out3, n3);
endmodule
