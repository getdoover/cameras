
const fs = require('fs');
const path = require('path');


class ConcatenatePlugin {
  constructor(options) {
    this.source = options.source;
    this.destination = path.resolve(options.destination);
    this.name = options.name;
    this.ignore = options.ignore || [];
  }

  apply(compiler) {
    // Register a callback to be executed before emitting assets
    compiler.hooks.emit.tapAsync('ConcatenatePlugin', (compilation, callback) => {
      // Check if JavaScript files exist in source directory
      const files = fs.readdirSync(this.source)
        .filter((file) => {
          const fileName = path.basename(file);
          const fileExtension = path.extname(file);
          return fileExtension === '.js' && !this.ignore.includes(fileName);
        });

      // Delete files if they exist
      files.forEach((file) => {
        const filePath = path.join(this.source, file);
        fs.unlinkSync(filePath);
      });

      callback();
    });

    // Register a callback to be executed after all assets have been emitted
    compiler.hooks.afterEmit.tapAsync('ConcatenatePlugin', (compilation, callback) => {
      // Read JavaScript files from source directory
      const files = fs.readdirSync(this.source)
        .filter((file) => {
          const fileName = path.basename(file);
          const fileExtension = path.extname(file);
          return fileExtension === '.js' && !this.ignore.includes(fileName);
        })
        .sort(); // Sort files alphabetically

      // Concatenate contents of all files
      const contents = files.map((file) => {
        const filePath = path.join(this.source, file);
        return fs.readFileSync(filePath, 'utf8');
      }).join('');

      // Write concatenated output to destination directory
      const outputPath = path.join(this.destination, this.name);
      fs.writeFileSync(outputPath, contents);

      callback();
    });
  }
}

module.exports = ConcatenatePlugin;
