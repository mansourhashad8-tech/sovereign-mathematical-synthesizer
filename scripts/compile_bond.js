const solc=require("solc"),fs=require("fs"),path=require("path");
const root=path.resolve(__dirname,"..");
const src=fs.readFileSync(path.join(root,"contracts/SovereignBond.sol"),"utf8");
const input={language:"Solidity",sources:{"SovereignBond.sol":{content:src}},
 settings:{optimizer:{enabled:true,runs:200},outputSelection:{"*":{"*":["abi","evm.bytecode.object"]}}}};
const out=JSON.parse(solc.compile(JSON.stringify(input)));
const errs=(out.errors||[]).filter(e=>e.severity==="error");
if(errs.length){console.log("ERRORS:");errs.forEach(e=>console.log(e.formattedMessage));process.exit(1);}
for(const [name,c] of Object.entries(out.contracts["SovereignBond.sol"])){
  fs.writeFileSync(path.join(root,`contracts/${name}.abi.json`),JSON.stringify(c.abi,null,2));
  fs.writeFileSync(path.join(root,`contracts/${name}.bin`),c.evm.bytecode.object);
  console.log(name,"ok",c.evm.bytecode.object.length/2,"bytes");
}
