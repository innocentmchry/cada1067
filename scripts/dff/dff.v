module dff (
    input  wire CK,   // Clock
    input  wire RN,   // Asynchronous active-low reset
    input  wire SN,   // Asynchronous active-low set
    input  wire D,    // Data input
    output reg  Q     // Data output
);

always @(posedge CK or negedge RN or negedge SN) begin
    if (!RN)
        Q <= 1'b0;    // Reset has priority
    else if (!SN)
        Q <= 1'b1;    // Set
    else
        Q <= D;       // Normal DFF operation
end

endmodule