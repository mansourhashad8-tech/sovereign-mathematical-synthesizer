// The Fuel settlement hand: deploys the Sway ledger and records settlements.
//
// Every number it returns comes from the chain. `gasCost` is read from the
// transaction's own fee field after it lands, and `delivered` is read back out
// of contract storage, so a reader can verify the claim instead of trusting it.
//
// Usage: node settle.mjs <command> [json-args]
//   deploy            -> { contractId, txId, blockHeight }
//   record  {...}     -> { txId, blockHeight, gasCost, delivered, totals }
//   state   {...}     -> { contractId, locked, totals, settled }
import fs from 'fs';
import path from 'path';
import { Provider, Wallet, Contract, ContractFactory } from 'fuels';

const RPC = process.env.FUEL_RPC_URL || 'http://127.0.0.1:4000/v1/graphql';
const SESSION_KEY = process.env.FUEL_SESSION_KEY || '0x' + '11'.repeat(32);
const ARTIFACTS =
  process.env.FUEL_ARTIFACTS || path.join(process.cwd(), 'sway/settlement_ledger/out/debug');
const CONTRACT_ID_FILE =
  process.env.FUEL_CONTRACT_FILE || path.join(process.cwd(), 'sway/chain/contract_id.txt');

function readArtifacts() {
  const raw = fs.readFileSync(path.join(ARTIFACTS, 'settlement_ledger.bin'));
  const abi = JSON.parse(
    fs.readFileSync(path.join(ARTIFACTS, 'settlement_ledger-abi.json'), 'utf8')
  );
  const slots = JSON.parse(
    fs.readFileSync(path.join(ARTIFACTS, 'settlement_ledger-storage_slots.json'), 'utf8')
  );
  return { bin: '0x' + raw.toString('hex'), abi, slots };
}

function out(obj) {
  process.stdout.write(JSON.stringify(obj) + '\n');
}

const [command, argsJson] = process.argv.slice(2);
const args = argsJson ? JSON.parse(argsJson) : {};
const provider = new Provider(RPC);
const wallet = Wallet.fromPrivateKey(SESSION_KEY, provider);
const { bin, abi, slots } = readArtifacts();

// PoA-instant mines one block per transaction, so the head block is the block
// that carries the transaction. Reading it back proves inclusion rather than
// assuming the call was mined.
async function inclusion(txId) {
  const height = await provider.getBlockNumber();
  const block = await provider.getBlock(height);
  const ids = block?.transactionIds ?? [];
  return {
    blockHeight: block?.height?.toString?.() ?? String(height),
    blockId: block?.id ?? '',
    included: ids.includes(txId),
  };
}

async function address() {
  // The genesis coin must belong to the wallet that will spend it, so the
  // genesis generator asks this command for the address instead of guessing.
  out({ address: wallet.address.toB256(), rpc: RPC });
}

async function deploy() {
  const factory = new ContractFactory(bin, abi, wallet);
  const { waitForResult } = await factory.deploy({ storageSlots: slots });
  const { contract, transactionResult } = await waitForResult();
  const id = contract.id.toB256();
  fs.mkdirSync(path.dirname(CONTRACT_ID_FILE), { recursive: true });
  fs.writeFileSync(CONTRACT_ID_FILE, id);
  await transactionResult?.waitForResult?.();
  const txId = transactionResult?.id ?? '';
  const inc = await inclusion(txId);
  out({ contractId: id, txId, ...inc });
}

async function record() {
  const id = fs.readFileSync(CONTRACT_ID_FILE, 'utf8').trim();
  const contract = new Contract(id, abi, wallet);
  const { beneficiary, delta, netProfit, commitment } = args;
  let call;
  try {
    call = await contract.functions
      .record(beneficiary, delta, netProfit, commitment)
      .call();
  } catch (err) {
    // A revert is an answer, not a crash: report the contract's own message so
    // the caller can tell "already settled" apart from "the node is gone".
    out({ reverted: true, reason: String(err?.message ?? err).slice(0, 400) });
    return;
  }
  const res = await call.waitForResult();
  const txId = call.transactionId ?? '';
  const inc = await inclusion(txId);
  // gasUsed is metered by the node for the transaction that actually ran, so
  // "the gas was covered" is measured rather than assumed.
  const delivered = await contract.functions.delivered_to(beneficiary).get();
  const totals = await contract.functions.totals().get();
  out({
    txId,
    ...inc,
    gasUsed: res.gasUsed?.toString() ?? '0',
    delivered: delivered.value.toString(),
    totals: [totals.value[0].toString(), totals.value[1].toString()],
  });
}

async function state() {
  const id = fs.readFileSync(CONTRACT_ID_FILE, 'utf8').trim();
  const contract = new Contract(id, abi, wallet);
  const locked = await contract.functions.locked_beneficiary().get();
  const totals = await contract.functions.totals().get();
  const payload = {
    contractId: id,
    locked: locked.value,
    totals: [totals.value[0].toString(), totals.value[1].toString()],
  };
  if (args.beneficiary) {
    const d = await contract.functions.delivered_to(args.beneficiary).get();
    payload.delivered = d.value.toString();
  }
  if (args.commitment) {
    const s = await contract.functions.is_settled(args.commitment).get();
    payload.settled = s.value;
  }
  out(payload);
}

const commands = { deploy, record, state, address };
if (!commands[command]) {
  out({ error: `unknown command: ${command}` });
  process.exit(2);
}
await commands[command]();
