pragma circom 2.0.0;

include "../node_modules/circomlib/circuits/comparators.circom";
include "../node_modules/circomlib/circuits/poseidon.circom";
include "../node_modules/circomlib/circuits/bitify.circom";

// Proves that one local execution delivered value to the locked beneficiary.
//
// delta and netProfit are public because the settlement record has to be
// auditable: the verifier contract stores what the proof proved, not what the
// caller claimed. What stays private is the execution itself -- the step count
// and the gas spent -- and the commitment binds those to the public numbers, so
// the public figures cannot be lifted from a different run.
//
// The commitment is also the anti-replay device. The contract registers one
// commitment per intent, so a valid proof for one intent cannot be recorded
// against another.
template Settlement() {
    // -- public ------------------------------------------------------------
    signal input beneficiary;   // 160-bit address, packed as an integer
    signal input minProfit;     // the floor from the intent's constraints
    signal input delta;         // value units that reached the beneficiary
    signal input netProfit;     // proven net profit, same units
    signal input commitment;    // Poseidon over the five values below

    // -- private (witness) -------------------------------------------------
    signal input beneficiaryWitness;
    signal input stepCount;
    signal input totalGas;

    signal output ok;

    // The beneficiary is the locked one. 160 bits is the whole address, so
    // this is equality on the full address rather than a prefix.
    component beneficiaryEq = IsEqual();
    beneficiaryEq.in[0] <== beneficiary;
    beneficiaryEq.in[1] <== beneficiaryWitness;

    // Value actually moved. A zero delta is not a settlement.
    component deltaBits = Num2Bits(64);
    deltaBits.in <== delta;
    component deltaPositive = IsZero();
    deltaPositive.in <== delta;

    // The profit meets the floor the intent demanded.
    component profitFloor = GreaterEqThan(64);
    profitFloor.in[0] <== netProfit;
    profitFloor.in[1] <== minProfit;

    // The commitment binds every number above.
    component commitmentHash = Poseidon(5);
    commitmentHash.inputs[0] <== beneficiary;
    commitmentHash.inputs[1] <== delta;
    commitmentHash.inputs[2] <== netProfit;
    commitmentHash.inputs[3] <== stepCount;
    commitmentHash.inputs[4] <== totalGas;

    component commitmentEq = IsEqual();
    commitmentEq.in[0] <== commitment;
    commitmentEq.in[1] <== commitmentHash.out;

    // All four conditions must hold at once. Circom only permits quadratic
    // constraints, so the product is built in stages.
    signal deltaOk;
    deltaOk <== 1 - deltaPositive.out;
    signal a;
    a <== beneficiaryEq.out * deltaOk;
    signal b;
    b <== profitFloor.out * commitmentEq.out;
    ok <== a * b;
    ok === 1;
}

component main {public [beneficiary, minProfit, delta, netProfit, commitment]} = Settlement();
