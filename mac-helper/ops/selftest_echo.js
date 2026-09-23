// Used only by the self-test: returns its JSON argument unchanged, proving that arguments reach the script as data and are never
// interpreted as script source. It is not part of the operations the server can request.
function run(argv) {
  return JSON.stringify(JSON.parse(argv[0]));
}
