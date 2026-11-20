// Genesis materialisation for the sovereign Fuel chain.
//
// A Fuel node starts empty: the UTXO set holds no coins, so every wallet has a
// zero balance and cannot pay even a zero gas price (the transaction still needs
// an input coin to spend). The node therefore bootstraps from a snapshot that
// mints exactly one coin to the session key. That coin is the *only* funding the
// chain ever needs: it is not a sponsor and not user money, it is the chain's own
// genesis supply, the same way a testnet mints its first block.
//
// Usage: node genesis.mjs <out-dir> <session-address-b256> [amount]
import fs from 'fs';
import path from 'path';
import { defaultSnapshotConfigs, getRandomB256 } from 'fuels';

const [outDir, owner, amountArg] = process.argv.slice(2);
if (!outDir || !owner) {
  console.error('usage: node genesis.mjs <out-dir> <owner-b256> [amount]');
  process.exit(2);
}
const amount = BigInt(amountArg || '1000000000000000');

const snap = JSON.parse(JSON.stringify(defaultSnapshotConfigs));
const baseAsset =
  snap.chainConfig.consensus_parameters?.V2?.base_asset_id ??
  snap.chainConfig.consensus_parameters?.V1?.base_asset_id;

snap.stateConfig.coins = [
  {
    tx_id: getRandomB256(),
    output_index: 0,
    owner,
    amount: Number(amount),
    asset_id: baseAsset,
    tx_pointer_block_height: 0,
    tx_pointer_tx_idx: 0,
  },
];

fs.mkdirSync(outDir, { recursive: true });
fs.writeFileSync(
  path.join(outDir, 'chainConfig.json'),
  JSON.stringify(snap.chainConfig, null, 2)
);
fs.writeFileSync(
  path.join(outDir, 'stateConfig.json'),
  JSON.stringify(snap.stateConfig, null, 2)
);
fs.writeFileSync(
  path.join(outDir, 'metadata.json'),
  JSON.stringify(snap.metadata, null, 2)
);
console.log(
  JSON.stringify({ baseAsset, owner, amount: amount.toString(), outDir })
);
